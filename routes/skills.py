"""routes/skills.py — Skills CRUD API."""

import os

from flask import Blueprint, jsonify, request

from lib.log import get_logger

logger = get_logger(__name__)

skills_bp = Blueprint('skills', __name__)

# Default project root — same directory that contains server.py
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pp():
    """Get project_path from request args, falling back to project root."""
    explicit = None
    if request.is_json and request.json:
        explicit = request.json.get('project_path')
    if not explicit:
        explicit = request.args.get('project_path')
    return explicit or _PROJECT_ROOT


# ── Local Skills CRUD ──────────────────────────────────

@skills_bp.route('/api/skills', methods=['GET'])
def api_list_skills():
    from lib.skills import list_skills
    scope = request.args.get('scope', 'all')
    skills = list_skills(project_path=_pp(), scope=scope)
    # Strip filepath from response (internal detail)
    for s in skills:
        s.pop('filepath', None)
    return jsonify({'skills': skills})


@skills_bp.route('/api/skills/<skill_id>', methods=['GET'])
def api_get_skill(skill_id):
    from lib.skills import get_skill
    skill = get_skill(skill_id, project_path=_pp())
    if not skill:
        return jsonify({'error': 'Skill not found'}), 404
    skill.pop('filepath', None)
    return jsonify(skill)


@skills_bp.route('/api/skills', methods=['POST'])
def api_create_skill():
    from lib.skills import create_skill
    data = request.get_json(force=True)
    skill_name = data.get('name', 'Untitled')
    logger.info('[Skills] creating skill: %s (scope=%s)', skill_name, data.get('scope', 'global'))
    skill = create_skill(
        name=skill_name,
        description=data.get('description', ''),
        body=data.get('body', ''),
        tags=data.get('tags'),
        scope=data.get('scope', 'global'),
        project_path=_pp(),
    )
    logger.info('[Skills] created skill %s', skill.get('id', '?'))
    skill.pop('filepath', None)
    return jsonify(skill), 201


@skills_bp.route('/api/skills/<skill_id>', methods=['PUT'])
def api_update_skill(skill_id):
    from lib.skills import update_skill
    data = request.get_json(force=True)
    skill = update_skill(skill_id, data, project_path=_pp())
    if not skill:
        return jsonify({'error': 'Skill not found'}), 404
    skill.pop('filepath', None)
    return jsonify(skill)


@skills_bp.route('/api/skills/<skill_id>', methods=['DELETE'])
def api_delete_skill(skill_id):
    from lib.skills import delete_skill
    logger.warning('[Skills] deleting skill %s', skill_id)
    ok = delete_skill(skill_id, project_path=_pp())
    if not ok:
        logger.warning('[Skills] skill %s not found for deletion', skill_id)
    return jsonify({'deleted': ok}), (200 if ok else 404)


@skills_bp.route('/api/skills/merge', methods=['POST'])
def api_merge_skills():
    """Merge multiple skills into one. JSON body: skill_ids, name, description, body, tags?, scope?"""
    from lib.skills import merge_skills
    data = request.get_json(force=True)
    logger.info('[Skills] merging skills: %s → %s', data.get('skill_ids', []), data.get('name', '?'))
    try:
        result = merge_skills(
            skill_ids=data.get('skill_ids', []),
            name=data.get('name', 'Merged Skill'),
            description=data.get('description', ''),
            body=data.get('body', ''),
            tags=data.get('tags'),
            scope=data.get('scope', 'project'),
            project_path=_pp(),
        )
    except ValueError as e:
        logger.debug('[Skills] merge_skills validation error: %s', e, exc_info=True)
        return jsonify({'error': str(e)}), 400
    result['merged_skill'].pop('filepath', None)
    return jsonify(result), 201


@skills_bp.route('/api/skills/<skill_id>/toggle', methods=['POST'])
def api_toggle_skill(skill_id):
    from lib.skills import toggle_skill
    data = request.get_json(silent=True) or {}
    skill = toggle_skill(skill_id, enabled=data.get('enabled'), project_path=_pp())
    if not skill:
        return jsonify({'error': 'Skill not found'}), 404
    skill.pop('filepath', None)
    return jsonify(skill)
