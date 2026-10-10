"""Public discovery graph carries one account and skips completed paid work."""
import pytest
from lib.research import recipe
from tests.research.test_research_recipe import _Seams, _install
pytestmark = pytest.mark.unit


def test_public_graph_forwards_shared_account_and_resume_does_no_paid_work(tmp_path):
    seams = _Seams()
    restore = _install(seams)
    seen = []
    port = lambda **kwargs: None
    try:
        for name, original in (("_build_survey", seams.build_survey),
                               ("_generate_ideas", seams.generate_ideas),
                               ("_evaluate_result", seams.evaluate)):
            def wrapped(*args, _name=name, _original=original, model_budget_admit=None, **kwargs):
                seen.append((_name, model_budget_admit))
                assert model_budget_admit is port
                return _original(*args, **kwargs)
            setattr(recipe, name, wrapped)
        first = recipe.build_research_from_direction(
            "long-context compression", str(tmp_path), user_id=1,
            model_budget_admit=port)
        assert [name for name, _ in seen] == ["_build_survey", "_generate_ideas", "_evaluate_result"]
        seen.clear()
        second = recipe.build_research_from_direction(
            "long-context compression", str(tmp_path), user_id=1,
            model_budget_admit=port)
        assert second == first
        assert seen == []
        assert len(seams.persist_ideate_calls) == 1
    finally:
        restore()


def test_invalid_account_rejected_before_creating_workdir(tmp_path):
    target = tmp_path / "must-not-exist"
    with pytest.raises(TypeError, match="must be callable"):
        recipe.build_research_from_direction("synthetic", str(target), user_id=1,
                                              model_budget_admit=object())
    assert not target.exists()


def test_public_graph_budget_refusal_does_not_retry_or_publish(tmp_path):
    from lib.agent_core.execution_budget import model_budget_admission, ModelBudgetAdmissionDenied
    seams = _Seams()
    restore = _install(seams)
    task = {'id':'recipe-stop', 'config':{'executionLimits':{
        'max_tokens':0, 'max_tool_calls':None, 'deadline':None}}}
    port = model_budget_admission(task)
    calls = []
    def refused(*args, model_budget_admit, **kwargs):
        calls.append(1)
        model_budget_admit(input_tokens=1, max_output_tokens=1)
    recipe._build_survey = refused
    try:
        with pytest.raises(ModelBudgetAdmissionDenied) as error:
            recipe.build_research_from_direction('long-context compression', str(tmp_path),
                user_id=1, model_budget_admit=port)
        assert error.value.budget is task['_executionBudget']
        assert calls == [1]
        assert seams.ideate_calls == []
        assert seams.persist_survey_calls == seams.persist_ideate_calls == []
    finally:
        restore()
