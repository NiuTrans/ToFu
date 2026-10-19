//! Exact observed Python-owner semantics and iterative deep-tree lifetime.
use serde_json::Value;
use tofu_runtime::html_projection::{project, project_bounded, Error};

#[test]
fn canonical_python_projection_matches() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/python_html_projection.json")).unwrap();
    for (index, case) in fixture["cases"].as_array().unwrap().iter().enumerate() {
        let source = case["source"].as_str().unwrap();
        if case.get("error").is_some() {
            assert_eq!(
                project(source),
                Err(Error::NumericReference),
                "case {index}"
            );
        } else {
            assert_eq!(
                project(source).unwrap(),
                case["expected"].as_str().unwrap(),
                "case {index}: {source:?}"
            );
        }
    }
}

#[test]
fn caller_budget_refuses_expansion_without_partial_output_or_retained_state() {
    assert_eq!(project_bounded("", 0).unwrap(), "");
    assert_eq!(project_bounded("abc", 3).unwrap(), "abc");
    assert_eq!(project_bounded("abc", 2), Err(Error::ProjectionBudget));
    assert_eq!(project_bounded("<script>hidden</script>", 0).unwrap(), "");
    let nested = "<tr>".to_owned() + &"<td>".repeat(100) + "x" + &"</td>".repeat(100) + "</tr>";
    assert_eq!(project_bounded(&nested, 64), Err(Error::ProjectionBudget));
    assert_eq!(
        project_bounded("<tr><td>a|b</td></tr>", 10).unwrap(),
        "| a\\|b |"
    );
    assert_eq!(
        project_bounded("<h1>x</h1><h1>y</h1>", 5),
        Err(Error::ProjectionBudget)
    );
    assert_eq!(project("<h1>fresh</h1>").unwrap(), "# fresh");
}

#[test]
fn deep_tree_projection_and_disposal_never_recurse() {
    std::thread::Builder::new()
        .stack_size(128 * 1024)
        .spawn(|| {
            let source = "<div>".repeat(100_000) + "kept" + &"</orphan>".repeat(100_000);
            assert_eq!(project(&source).unwrap(), "kept");
            let cells =
                "<tr>".to_owned() + &"<td>".repeat(100_000) + &"</td>".repeat(100_000) + "</tr>";
            assert_eq!(
                project(&cells).unwrap(),
                "| ".to_owned() + &" | ".repeat(99_999) + " |"
            );
            assert_eq!(project("<h1>fresh</h1>").unwrap(), "# fresh");
        })
        .unwrap()
        .join()
        .unwrap();
}
