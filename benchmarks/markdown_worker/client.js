/** Experimental parser RPC: one running + one newest waiting source, explicit teardown.
 * This is fixture code, not a production renderer or conversation authority.
 */
globalThis.createParserWorkerExperiment = async function (workerSource) {
  const url = URL.createObjectURL(new Blob([workerSource], {type: 'text/javascript'}));
  const worker = new Worker(url);
  let running = null, waiting = null, disposed = false, sequence = 0;
  let readyResolve, readyReject;
  const ready = new Promise((resolve, reject) => { readyResolve=resolve; readyReject=reject; });
  const abort = () => new DOMException('Parser request superseded or disposed', 'AbortError');
  function send(job) {
    running=job;
    worker.postMessage({id:job.id,text:job.text,escapeRawHtml:job.escapeRawHtml});
  }
  worker.onmessage = ({data}) => {
    if (data.ready) { readyResolve(); return; }
    if (!running || data.id !== running.id || disposed) return;
    const completed=running;running=null;
    if (completed.id!==sequence) completed.reject(abort());
    else if (data.error) completed.reject(new Error(data.error)); else completed.resolve(data);
    if (waiting) { const next=waiting;waiting=null;send(next); }
  };
  worker.onerror = event => {
    event.preventDefault();
    const error=new Error(event.message || 'Parser worker failed');
    disposed=true;worker.terminate();
    readyReject(error);
    running?.reject(error);waiting?.reject(error);
    running=null;waiting=null;
  };
  const timeout=setTimeout(() => readyReject(new Error('Parser worker startup timed out')),10000);
  try { await ready; } catch(error) { worker.terminate();throw error; }
  finally { clearTimeout(timeout);URL.revokeObjectURL(url); }
  return {
    parse(text,escapeRawHtml=false) {
      if (disposed) return Promise.reject(abort());
      return new Promise((resolve,reject) => {
        const job={id:++sequence,text,escapeRawHtml,resolve,reject};
        if (running) { waiting?.reject(abort());waiting=job; } else send(job);
      });
    },
    dispose() {
      disposed=true;worker.terminate();running?.reject(abort());waiting?.reject(abort());
      running=null;waiting=null;
    },
    pendingCount() { return Number(Boolean(running))+Number(Boolean(waiting)); },
  };
};
