"""Direct test: Opus 4.7 + xhigh/max depth with the FULL cat puzzle (clues included)."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ['LLM_DEBUG_RAW_SSE'] = 'opus'  # dump raw SSE to logs/raw_sse.log

from lib.llm_client import build_body, stream_chat

MODEL = 'aws.claude-opus-4.7'
PROMPT = (
    "Three people — Alice, Bob, and Carol — each own exactly one pet "
    "(a cat, a dog, or a fish) and live in exactly one city "
    "(Paris, Tokyo, or Lima). From the clues below, determine who "
    "owns what and lives where:\n\n"
    "1. The dog owner does not live in Paris.\n"
    "2. Carol does not own the fish.\n"
    "3. Alice lives in Tokyo.\n"
    "4. The person in Lima owns the cat.\n"
    "5. Bob does not live in Lima.\n\n"
    "Show your reasoning step by step, then give the final assignment."
)

messages = [{'role': 'user', 'content': PROMPT}]

for depth in ['xhigh', 'max']:
    print(f'\n========== depth={depth} ==========')
    body = build_body(model=MODEL, messages=messages, stream=True,
                      thinking_enabled=True, thinking_depth=depth, effort=depth)
    print(f'body.thinking    = {body.get("thinking")}')
    print(f'body.effort      = {body.get("effort")}')
    print(f'body.temperature = {body.get("temperature")}')

    state = {'thinking': 0, 'content': 0}
    def _th(chunk, s=state): s['thinking'] += len(chunk)
    def _ct(chunk, s=state): s['content']  += len(chunk)

    t0 = time.time()
    try:
        msg, finish, usage = stream_chat(body, on_thinking=_th, on_content=_ct,
                                         log_prefix=f'[test-{depth}] ')
        dt = time.time() - t0
        print(f'elapsed={dt:.1f}s  thinking={state["thinking"]}  content={state["content"]}  finish={finish}')
        reason = (msg or {}).get('reasoning_content') or ''
        content = (msg or {}).get('content') or ''
        print(f'msg.reasoning_content len = {len(reason)}')
        print(f'msg.content len           = {len(content)}')
        if reason: print(f'REASONING PREVIEW: {reason[:400]!r}')
        if content: print(f'CONTENT PREVIEW:   {content[:300]!r}')
    except Exception as e:
        print(f'ERROR: {type(e).__name__}: {e}')
