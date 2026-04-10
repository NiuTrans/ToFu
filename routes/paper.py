"""routes/paper.py — Paper Reading Mode endpoints.

Provides:
- /api/paper/report    — Generate structured analysis report for a paper via LLM
- /api/paper/fetch-arxiv — Fetch PDF from arXiv URL and return a serveable path
"""

import os
import re
import threading
import time
import uuid

import requests
from flask import Blueprint, Response, jsonify, request, send_file

from lib.log import get_logger
from lib.llm_client import chat

logger = get_logger(__name__)

paper_bp = Blueprint('paper', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER_DIR = os.path.join(BASE_DIR, 'uploads', 'papers')
os.makedirs(PAPER_DIR, exist_ok=True)

# ── In-memory task store for async report generation ──
_report_tasks = {}
_report_tasks_lock = threading.Lock()
_REPORT_TASK_TTL = 3600  # 1 hour


# ══════════════════════════════════════════════════════
#  Paper analysis report template
# ══════════════════════════════════════════════════════

REPORT_TEMPLATE = """You are an expert academic paper analyst. Analyze the following paper and produce a structured report in the EXACT format below. Write in the same language as the paper (if the paper is in Chinese, write in Chinese; if in English, write in English).

## 📋 Basic Information
- **Title**: (full paper title)
- **Authors**: (all authors, with affiliations if available)
- **Year / Venue**: (publication year, journal or conference name)

## 🎯 Research Problem
- What problem does this paper address?
- Why is this problem important?
- What gap in existing research does it fill?

## 💡 Core Methodology
- Describe the proposed approach/method in detail
- Key algorithms, models, or theoretical frameworks
- Include references to key equations or figures by section number if applicable

## 📊 Experiments & Results
- Datasets used
- Baselines compared against
- Key quantitative results (accuracy, F1, speedup, etc.)
- Most important tables/figures and what they show

## 🔬 Key Contributions & Limitations
- List 3-5 core contributions claimed by the authors
- Limitations acknowledged by the authors or apparent from the work
- Potential threats to validity

## 🔗 Related Work & Context
- What prior work does this build upon?
- How does it differ from the closest related approaches?
- Where does it fit in the broader research landscape?

## ⭐ One-Sentence Summary
(A single sentence capturing the essence of the paper)

## 📝 Reading Notes
- Key terms or concepts that a reader should understand
- Suggested follow-up papers or resources

---

Here is the full text of the paper:

{paper_text}"""


def _cleanup_report_tasks():
    """Remove expired report tasks."""
    now = time.time()
    with _report_tasks_lock:
        expired = [tid for tid, t in _report_tasks.items()
                   if t.get('status') != 'running'
                   and now - t.get('completed_at', now) > _REPORT_TASK_TTL]
        for tid in expired:
            del _report_tasks[tid]
        if expired:
            logger.debug('[Paper] Cleaned up %d expired report tasks', len(expired))


def _generate_report_worker(task_id, paper_text, model=None):
    """Background worker: call LLM to generate paper analysis report."""
    try:
        with _report_tasks_lock:
            task = _report_tasks.get(task_id)
            if not task:
                return
            task['status'] = 'running'

        # Truncate very long papers to avoid token limits
        max_chars = 120000
        if len(paper_text) > max_chars:
            paper_text = paper_text[:max_chars] + '\n\n[... truncated for length ...]'

        prompt = REPORT_TEMPLATE.format(paper_text=paper_text)
        messages = [{'role': 'user', 'content': prompt}]

        result_text = ''
        for chunk in chat(messages, model=model, stream=True):
            if isinstance(chunk, dict):
                delta = chunk.get('choices', [{}])[0].get('delta', {}).get('content', '')
            else:
                delta = chunk
            if delta:
                result_text += delta
                with _report_tasks_lock:
                    task = _report_tasks.get(task_id)
                    if task:
                        task['partial'] = result_text

        with _report_tasks_lock:
            task = _report_tasks.get(task_id)
            if task:
                task['status'] = 'done'
                task['result'] = result_text
                task['completed_at'] = time.time()
                logger.info('[Paper] Report generated for task %s, %d chars', task_id, len(result_text))

    except Exception as e:
        logger.error('[Paper] Report generation failed for task %s: %s', task_id, e, exc_info=True)
        with _report_tasks_lock:
            task = _report_tasks.get(task_id)
            if task:
                task['status'] = 'error'
                task['error'] = str(e)
                task['completed_at'] = time.time()


# ══════════════════════════════════════════════════════
#  API Endpoints
# ══════════════════════════════════════════════════════

@paper_bp.route('/api/paper/chat', methods=['POST'])
def paper_chat():
    """Simple streaming LLM chat for paper Q&A.

    Body JSON:
        messages: list — OpenAI-format messages [{role, content}, ...]
        model: str (optional) — LLM model to use
    Returns:
        SSE stream of chat completions.
    """
    data = request.get_json(silent=True) or {}
    messages = data.get('messages', [])
    model = data.get('model') or None

    if not messages:
        return jsonify({'error': 'No messages provided'}), 400

    def generate():
        try:
            for chunk in chat(messages, model=model, stream=True):
                if isinstance(chunk, dict):
                    import json as _json
                    yield f'data: {_json.dumps(chunk)}\n\n'
                elif isinstance(chunk, str):
                    import json as _json
                    yield f'data: {_json.dumps({"choices": [{"delta": {"content": chunk}}]})}\n\n'
        except Exception as e:
            logger.error('[Paper] Chat streaming error: %s', e, exc_info=True)
            import json as _json
            yield f'data: {_json.dumps({"error": str(e)})}\n\n'
        yield 'data: [DONE]\n\n'

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@paper_bp.route('/api/paper/report', methods=['POST'])
def start_report():
    """Start async paper analysis report generation.

    Body JSON:
        paper_text: str — full text of the paper
        model: str (optional) — LLM model to use
    Returns:
        { task_id: str }
    """
    _cleanup_report_tasks()
    data = request.get_json(silent=True) or {}
    paper_text = data.get('paper_text', '').strip()
    if not paper_text:
        return jsonify({'error': 'No paper_text provided'}), 400
    if len(paper_text) < 100:
        return jsonify({'error': 'Paper text too short (< 100 chars)'}), 400

    model = data.get('model') or None
    task_id = str(uuid.uuid4())[:12]

    with _report_tasks_lock:
        _report_tasks[task_id] = {
            'status': 'queued',
            'created_at': time.time(),
            'partial': '',
            'result': None,
            'error': None,
        }

    t = threading.Thread(target=_generate_report_worker, args=(task_id, paper_text, model),
                         daemon=True, name=f'paper-report-{task_id}')
    t.start()
    logger.info('[Paper] Started report generation task %s (%d chars)', task_id, len(paper_text))
    return jsonify({'ok': True, 'task_id': task_id})


@paper_bp.route('/api/paper/report/<task_id>', methods=['GET'])
def get_report(task_id):
    """Poll report generation status.

    Returns:
        { status: 'queued'|'running'|'done'|'error', partial?: str, result?: str, error?: str }
    """
    with _report_tasks_lock:
        task = _report_tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404
    resp = {'status': task['status']}
    if task.get('partial'):
        resp['partial'] = task['partial']
    if task.get('result'):
        resp['result'] = task['result']
    if task.get('error'):
        resp['error'] = task['error']
    return jsonify(resp)


@paper_bp.route('/api/paper/report/<task_id>/stream', methods=['GET'])
def stream_report(task_id):
    """SSE stream for report generation — sends partial updates as they arrive."""
    with _report_tasks_lock:
        task = _report_tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404

    def generate():
        last_len = 0
        while True:
            with _report_tasks_lock:
                t = _report_tasks.get(task_id)
            if not t:
                yield 'data: {"done": true}\n\n'
                break
            partial = t.get('partial', '')
            if len(partial) > last_len:
                new_text = partial[last_len:]
                last_len = len(partial)
                import json
                yield f'data: {json.dumps({"text": new_text})}\n\n'
            if t['status'] in ('done', 'error'):
                import json
                if t['status'] == 'error':
                    yield f'data: {json.dumps({"error": t.get("error", "Unknown")})}\n\n'
                yield 'data: {"done": true}\n\n'
                break
            time.sleep(0.3)

    return Response(generate(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@paper_bp.route('/api/paper/fetch-arxiv', methods=['POST'])
def fetch_arxiv():
    """Download PDF from arXiv URL and serve it locally.

    Body JSON:
        url: str — arXiv URL (abs page, pdf link, or just the ID like 2301.12345)
    Returns:
        { ok: true, pdf_url: str, title: str, arxiv_id: str }
    """
    data = request.get_json(silent=True) or {}
    url_input = data.get('url', '').strip()
    if not url_input:
        return jsonify({'error': 'No URL provided'}), 400

    # Extract arXiv ID from various URL formats
    arxiv_id = _extract_arxiv_id(url_input)
    if not arxiv_id:
        return jsonify({'error': 'Could not parse arXiv ID from URL'}), 400

    pdf_url = f'https://arxiv.org/pdf/{arxiv_id}.pdf'
    filename = f'arxiv_{arxiv_id.replace("/", "_")}.pdf'
    filepath = os.path.join(PAPER_DIR, filename)

    # Check if already downloaded
    if os.path.exists(filepath) and os.path.getsize(filepath) > 1000:
        logger.info('[Paper] arXiv %s already cached at %s', arxiv_id, filepath)
        return jsonify({
            'ok': True,
            'pdf_url': f'/api/paper/pdf/{filename}',
            'arxiv_id': arxiv_id,
            'cached': True,
        })

    # Download the PDF
    try:
        logger.info('[Paper] Downloading arXiv PDF: %s', pdf_url)
        resp = requests.get(pdf_url, timeout=60, stream=True,
                            headers={'User-Agent': 'Mozilla/5.0 (compatible; TofuBot/1.0)'})
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '')
        if 'pdf' not in content_type and 'octet-stream' not in content_type:
            logger.warning('[Paper] Unexpected content type from arXiv: %s', content_type)

        with open(filepath, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        file_size = os.path.getsize(filepath)
        logger.info('[Paper] Downloaded arXiv %s: %d bytes', arxiv_id, file_size)

        return jsonify({
            'ok': True,
            'pdf_url': f'/api/paper/pdf/{filename}',
            'arxiv_id': arxiv_id,
            'file_size': file_size,
        })

    except requests.Timeout:
        logger.warning('[Paper] arXiv download timeout: %s', pdf_url)
        return jsonify({'error': 'Download timed out (60s)'}), 504
    except requests.RequestException as e:
        logger.warning('[Paper] arXiv download failed: %s — %s', pdf_url, e)
        return jsonify({'error': f'Download failed: {str(e)}'}), 502


@paper_bp.route('/api/paper/pdf/<filename>')
def serve_paper_pdf(filename):
    """Serve a downloaded paper PDF."""
    # Sanitize filename
    filename = os.path.basename(filename)
    filepath = os.path.join(PAPER_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({'error': 'PDF not found'}), 404
    return send_file(filepath, mimetype='application/pdf')


@paper_bp.route('/api/paper/upload', methods=['POST'])
def upload_paper():
    """Upload a PDF file for paper reading mode.

    Returns:
        { ok: true, pdf_url: str, filename: str }
    """
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'No filename'}), 400
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Only PDF files are supported'}), 400

    filename = f"{int(time.time() * 1000)}_{file.filename}"
    # Sanitize
    filename = re.sub(r'[^\w\-.]', '_', filename)
    filepath = os.path.join(PAPER_DIR, filename)

    try:
        file.save(filepath)
        file_size = os.path.getsize(filepath)
        logger.info('[Paper] Uploaded paper: %s (%d bytes)', filename, file_size)
        return jsonify({
            'ok': True,
            'pdf_url': f'/api/paper/pdf/{filename}',
            'filename': filename,
            'file_size': file_size,
        })
    except Exception as e:
        logger.error('[Paper] Upload failed: %s', e, exc_info=True)
        return jsonify({'error': f'Upload failed: {str(e)}'}), 500


def _extract_arxiv_id(url_or_id):
    """Extract arXiv paper ID from various URL formats.

    Supports:
        - 2301.12345
        - 2301.12345v2
        - arxiv.org/abs/2301.12345
        - arxiv.org/pdf/2301.12345
        - arxiv.org/pdf/2301.12345.pdf
        - arxiv.org/abs/hep-th/0601001
        - https://arxiv.org/abs/2301.12345
    """
    url_or_id = url_or_id.strip()

    # Direct ID pattern (new format: YYMM.NNNNN)
    m = re.match(r'^(\d{4}\.\d{4,5})(v\d+)?$', url_or_id)
    if m:
        return m.group(1) + (m.group(2) or '')

    # Old format ID: category/NNNNNNN
    m = re.match(r'^([a-z-]+/\d{7})(v\d+)?$', url_or_id)
    if m:
        return m.group(1) + (m.group(2) or '')

    # URL patterns
    m = re.search(r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)', url_or_id)
    if m:
        return m.group(1)

    m = re.search(r'arxiv\.org/(?:abs|pdf)/([a-z-]+/\d{7}(?:v\d+)?)', url_or_id)
    if m:
        return m.group(1)

    return None
