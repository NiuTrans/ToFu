"""routes/errors.py — Error summary & resolution tracking endpoints.

Extracted from routes/common.py for better separation of concerns.
All endpoints use lib.project_error_tracker with the app's own root.
"""

import os

from flask import Blueprint, jsonify, make_response, request

from lib.log import get_logger

logger = get_logger(__name__)

errors_bp = Blueprint('errors', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@errors_bp.route('/api/errors/recent', methods=['GET'])
def recent_errors():
    from lib.project_error_tracker import enrich_errors, scan_project_errors
    n = min(int(request.args.get('n', 500)), 5000)
    errors = scan_project_errors(BASE_DIR, n=n, error_only=True)
    if request.args.get('enriched', '1') == '1':
        try:
            errors = enrich_errors(BASE_DIR, errors)
        except Exception as e:
            logger.warning('[errors/recent] Enrichment failed (returning raw): %s', e)
    return jsonify({'errors': errors, 'count': len(errors)})


@errors_bp.route('/api/errors/digest', methods=['GET'])
def error_digest():
    from lib.project_error_tracker import daily_digest, enrich_errors
    digest = daily_digest(BASE_DIR)
    try:
        if digest.get('recent_errors'):
            digest['recent_errors'] = enrich_errors(BASE_DIR, digest['recent_errors'])
    except Exception as e:
        logger.warning('[errors/digest] Enrichment failed: %s', e)
    return jsonify(digest)


@errors_bp.route('/api/errors/resolve', methods=['POST'])
def resolve_error():
    data = request.get_json(silent=True) or {}
    fp = data.get('fingerprint', '').strip()
    if not fp:
        return jsonify({'ok': False, 'error': 'fingerprint is required'}), 400
    try:
        from lib.project_error_tracker import mark_resolved
        resolution = mark_resolved(
            BASE_DIR, fp,
            resolved_by=data.get('resolved_by', ''),
            ticket=data.get('ticket', ''),
            notes=data.get('notes', ''),
            logger_name=data.get('logger_name', ''),
            sample_message=data.get('sample_message', ''),
        )
        return jsonify({'ok': True, 'resolution': resolution})
    except Exception as e:
        logger.error('[errors/resolve] Failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': str(e)}), 500


@errors_bp.route('/api/errors/unresolve', methods=['POST'])
def unresolve_error():
    data = request.get_json(silent=True) or {}
    fp = data.get('fingerprint', '').strip()
    if not fp:
        return jsonify({'ok': False, 'error': 'fingerprint is required'}), 400
    try:
        from lib.project_error_tracker import mark_unresolved
        deleted = mark_unresolved(BASE_DIR, fp, reason=data.get('reason', ''))
        return jsonify({'ok': True, 'deleted': deleted})
    except Exception as e:
        logger.error('[errors/unresolve] Failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': str(e)}), 500


@errors_bp.route('/api/errors/resolutions', methods=['GET'])
def list_resolutions():
    try:
        from lib.project_error_tracker import get_resolutions_list
        resolutions = get_resolutions_list(BASE_DIR)
        return jsonify({'resolutions': resolutions, 'count': len(resolutions)})
    except Exception as e:
        logger.error('[errors/resolutions] Failed: %s', e, exc_info=True)
        return jsonify({'resolutions': [], 'count': 0, 'error': str(e)}), 500


@errors_bp.route('/api/errors/unresolved', methods=['GET'])
def unresolved_errors():
    n = min(int(request.args.get('n', 2000)), 10000)
    try:
        from lib.project_error_tracker import get_unresolved_grouped
        grouped = get_unresolved_grouped(BASE_DIR, n=n)
        return jsonify({
            'groups': grouped,
            'total_groups': len(grouped),
            'total_occurrences': sum(g['count'] for g in grouped),
        })
    except Exception as e:
        logger.error('[errors/unresolved] Failed: %s', e, exc_info=True)
        return jsonify({'groups': [], 'total_groups': 0,
                        'total_occurrences': 0, 'error': str(e)}), 500


@errors_bp.route('/api/errors/stats', methods=['GET'])
def error_stats_endpoint():
    n = min(int(request.args.get('n', 2000)), 10000)
    try:
        from lib.project_error_tracker import error_stats
        stats = error_stats(BASE_DIR, n=n)
        return jsonify(stats)
    except Exception as e:
        logger.error('[errors/stats] Failed: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500


@errors_bp.route('/api/errors/resolve-by-logger', methods=['POST'])
def resolve_by_logger_endpoint():
    data = request.get_json(silent=True) or {}
    logger_name = data.get('logger_name', '').strip()
    if not logger_name:
        return jsonify({'ok': False, 'error': 'logger_name is required'}), 400
    try:
        from lib.project_error_tracker import resolve_by_logger
        count = resolve_by_logger(
            BASE_DIR, logger_name,
            resolved_by=data.get('resolved_by', ''),
            ticket=data.get('ticket', ''),
            notes=data.get('notes', ''),
        )
        return jsonify({'ok': True, 'resolved_count': count})
    except Exception as e:
        logger.error('[errors/resolve-by-logger] Failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': str(e)}), 500


@errors_bp.route('/api/errors/resolve-by-pattern', methods=['POST'])
def resolve_by_pattern_endpoint():
    data = request.get_json(silent=True) or {}
    pattern = data.get('pattern', '').strip()
    if not pattern:
        return jsonify({'ok': False, 'error': 'pattern is required'}), 400
    try:
        from lib.project_error_tracker import resolve_by_message_pattern
        count = resolve_by_message_pattern(
            BASE_DIR, pattern,
            resolved_by=data.get('resolved_by', ''),
            ticket=data.get('ticket', ''),
            notes=data.get('notes', ''),
        )
        return jsonify({'ok': True, 'resolved_count': count})
    except Exception as e:
        logger.error('[errors/resolve-by-pattern] Failed: %s', e, exc_info=True)
        return jsonify({'ok': False, 'error': str(e)}), 500


@errors_bp.route('/api/support-bundle', methods=['GET'])
def support_bundle():
    """Return a sanitized diagnostic bundle for bug reports.

    Includes system info, recent errors, active config (no secrets),
    and log correlation guidance. Users paste this into GitHub issues.
    """
    import platform
    import subprocess

    try:
        import flask
    except ImportError:
        flask = None

    bundle = {}

    # ── System info ──
    bundle['system'] = {
        'python_version': platform.python_version(),
        'platform': platform.platform(),
        'flask_version': getattr(flask, '__version__', 'unknown'),
    }

    # ── Git hash (if available) ──
    try:
        git_hash = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=BASE_DIR, timeout=5, stderr=subprocess.DEVNULL,
        ).decode().strip()
        bundle['system']['git_hash'] = git_hash
    except Exception as e:
        logger.debug('support_bundle: git hash unavailable: %s', e)
        bundle['system']['git_hash'] = 'unknown'

    # ── Active config (model names, feature flags — NO api keys) ──
    try:
        from lib import LLM_BASE_URL, LLM_MODEL
        bundle['config'] = {
            'default_model': LLM_MODEL,
            'base_url_domain': LLM_BASE_URL.split('/')[2] if '/' in LLM_BASE_URL else 'unknown',
        }
    except Exception as e:
        logger.debug('support_bundle: config extraction failed: %s', e)
        bundle['config'] = {}

    try:
        features_path = os.path.join(BASE_DIR, 'data', 'config', 'features.json')
        if os.path.isfile(features_path):
            import json
            with open(features_path) as f:
                features = json.load(f)
            # Only include feature flags, not any secrets
            bundle['config']['features'] = {
                k: v for k, v in features.items()
                if isinstance(v, (bool, int, str)) and 'key' not in k.lower()
                and 'token' not in k.lower() and 'secret' not in k.lower()
                and 'password' not in k.lower()
            }
    except Exception as e:
        logger.debug('support_bundle: features extraction failed: %s', e)

    # ── Recent errors (sanitized, last 50) ──
    try:
        from lib.project_error_tracker import scan_project_errors
        errors = scan_project_errors(BASE_DIR, n=500, error_only=True)
        # Sanitize: remove any field that might contain secrets
        sanitized = []
        for err in errors[:50]:
            entry = {
                'timestamp': err.get('timestamp', ''),
                'level': err.get('level', ''),
                'logger': err.get('logger', ''),
                'message': err.get('message', '')[:500],  # truncate
                'fingerprint': err.get('fingerprint', ''),
            }
            # Scrub potential secrets from message
            msg = entry['message']
            for pattern in ['Bearer ', 'sk-', 'token=', 'key=', 'password=']:
                if pattern.lower() in msg.lower():
                    idx = msg.lower().index(pattern.lower())
                    msg = msg[:idx + len(pattern)] + '***REDACTED***'
                    entry['message'] = msg
                    break
            sanitized.append(entry)
        bundle['recent_errors'] = sanitized
        bundle['error_count'] = len(errors)
    except Exception as e:
        logger.warning('support_bundle: error scan failed: %s', e)
        bundle['recent_errors'] = []
        bundle['error_count'] = 0

    # ── Request ID guidance ──
    bundle['log_correlation'] = {
        'hint': 'Use the request ID (rid:XXXX) from error messages to grep logs/app.log for the full request trace.',
        'log_files': ['logs/app.log', 'logs/error.log'],
    }

    return jsonify(bundle)


@errors_bp.route('/api/errors/export', methods=['GET'])
def export_errors():
    fmt = request.args.get('format', 'json')
    n = min(int(request.args.get('n', 2000)), 10000)
    try:
        from lib.project_error_tracker import export_for_pm
        content = export_for_pm(BASE_DIR, n=n, format=fmt)
        content_types = {
            'json': 'application/json',
            'markdown': 'text/markdown',
            'csv': 'text/csv',
        }
        ct = content_types.get(fmt, 'application/json')
        resp = make_response(content)
        resp.headers['Content-Type'] = f'{ct}; charset=utf-8'
        if fmt == 'csv':
            resp.headers['Content-Disposition'] = 'attachment; filename=unresolved_errors.csv'
        elif fmt == 'markdown':
            resp.headers['Content-Disposition'] = 'attachment; filename=unresolved_errors.md'
        return resp
    except Exception as e:
        logger.error('[errors/export] Failed: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 500
