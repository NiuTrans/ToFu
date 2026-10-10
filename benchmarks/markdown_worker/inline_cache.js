/** Owner-scoped inline-lexer experiment. Full block lexing and rendering remain canonical.
 * No global Marked patch, DOM authority or production default is installed.
 */
globalThis.createInlineCacheExperiment = function ({ownerId, maximumBytes, getEpoch, createLexer, renderTokens}) {
  if (!Number.isSafeInteger(ownerId) || ownerId < 1 || !Number.isSafeInteger(maximumBytes) || maximumBytes < 1)
    throw new TypeError('Explicit owner and positive byte budget required');
  if (![getEpoch,createLexer,renderTokens].every(value=>typeof value==='function'))
    throw new TypeError('Explicit policy, lexer and renderer owners required');
  const entries=new Map();let retained=0,epoch=null,closed=false,active=false,hits=0,misses=0;
  function authorize(owner) { if (owner!==ownerId || !Number.isSafeInteger(owner)) throw new Error('Foreign parser owner'); }
  function clear() { entries.clear();retained=0; }
  function plainJSON(value) {
    if (value===null || ['string','boolean'].includes(typeof value)) return true;
    if (typeof value==='number') return Number.isFinite(value);
    if (Array.isArray(value)) return value.every(plainJSON);
    if (typeof value!=='object') return false;
    const prototype=Object.getPrototypeOf(value);
    return (prototype===Object.prototype || prototype===null) && Object.values(value).every(plainJSON);
  }
  return {
    parse(owner, source) {
      authorize(owner);
      if (closed || active) throw new Error('Parser closed or recursively active');
      active=true;hits=0;misses=0;
      try {
        const current=getEpoch();
        if (!Number.isSafeInteger(current) || current<0) throw new TypeError('Explicit nonnegative policy epoch required');
        if (epoch!==current) { clear();epoch=current; }
        const lexer=createLexer();const original=lexer.inlineTokens.bind(lexer);
        lexer.inlineTokens=(text,tokens=[])=>{
          // Existing tokens can affect text merging. Custom non-JSON tokens
          // stay canonical rather than being lossy-serialized into a cache.
          if (tokens.length || !plainJSON(lexer.state) || !plainJSON(lexer.tokens.links)) return original(text,tokens);
          const key=JSON.stringify([text,lexer.tokens.links,lexer.state]);
          const entry=entries.get(key);
          if (entry) {
            const restored=JSON.parse(entry.payload);
            tokens.push(...restored.tokens);Object.assign(lexer.state,restored.state);
            entries.delete(key);entries.set(key,entry);hits++;
            return tokens;
          }
          misses++;
          const result=original(text,tokens);
          if (!closed && plainJSON(result) && plainJSON(lexer.state)) {
            const payload=JSON.stringify({tokens:result,state:lexer.state});
            const bytes=(key.length+payload.length)*2+256;
            if (bytes<=maximumBytes) {
              const prior=entries.get(key);
              if (prior) { retained-=prior.bytes;entries.delete(key); }
              while (retained+bytes>maximumBytes && entries.size) {
                const oldest=entries.keys().next().value;retained-=entries.get(oldest).bytes;entries.delete(oldest);
              }
              entries.set(key,{payload,bytes});retained+=bytes;
            }
          }
          return result;
        };
        const result=renderTokens(lexer.lex(source));
        if (closed) throw new Error('Parser disposed during parse');
        return result;
      } finally { active=false; }
    },
    snapshot(owner) { authorize(owner);return {retainedBytes:retained,entries:entries.size,hits,misses,closed}; },
    dispose(owner) { authorize(owner);closed=true;clear(); },
  };
};
