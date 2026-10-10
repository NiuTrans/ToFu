//! Current Python local-counter parity, exact tool spelling and admission bounds.
use serde_json::{json, Value};
use tofu_runtime::token_ingress::{tool_text, Bounds, Counter, Encoder, Encoding, Error};
fn bounds() -> Bounds {
    Bounds {
        input_bytes: 1048576,
        text_bytes: 1048576,
        tool_json_bytes: 1048576,
        depth: 128,
    }
}
#[test]
fn actual_python_counter_and_tool_json_match_both_encodings() {
    let rows: Value =
        serde_json::from_str(include_str!("fixtures/python_token_ingress.json")).unwrap();
    let cl = Encoder::new(Encoding::Cl100k);
    let o = Encoder::new(Encoding::O200k);
    assert!(!cl.loaded() && !o.loaded());
    for (i, row) in rows.as_array().unwrap().iter().enumerate() {
        let encoder = if row["encoding"] == "cl100k_base" {
            cl.clone()
        } else {
            o.clone()
        };
        let c = Counter::new(
            7,
            "task".into(),
            row["model"].as_str().unwrap().into(),
            encoder,
            bounds(),
        )
        .unwrap();
        if let Some(tools) = row["template"].get("tools") {
            assert_eq!(
                tool_text(tools, 1048576, 128).unwrap(),
                row["tools_text"].as_str().unwrap(),
                "tool spellings: {i}"
            );
        }
        let count = c
            .count(
                7,
                "task",
                row["messages"].as_array().unwrap(),
                row["template"].as_object().unwrap(),
            )
            .unwrap();
        assert_eq!(count, row["tokens"].as_u64().unwrap(), "case {i}: {row}");
    }
    assert!(cl.loaded() && o.loaded());
}
#[test]
fn foreign_or_oversized_inputs_fail_without_initializing_encoder() {
    let encoder = Encoder::new(Encoding::Cl100k);
    let mut b = bounds();
    b.text_bytes = 4;
    let c = Counter::new(7, "task".into(), "fixture".into(), encoder.clone(), b).unwrap();
    let template = json!({"model":"fixture"}).as_object().unwrap().clone();
    assert_eq!(c.count(8, "task", &[], &template), Err(Error::Identity));
    assert_eq!(c.count(7, "foreign", &[], &template), Err(Error::Identity));
    assert_eq!(
        c.count(
            7,
            "task",
            &[json!({"role":"user","content":"too large"})],
            &template
        ),
        Err(Error::InputBytes)
    );
    assert!(!encoder.loaded());
    assert_eq!(
        tool_text(&json!({"large":"x".repeat(100)}), 10, 128),
        Err(Error::ToolBytes)
    );
    assert_eq!(
        tool_text(&json!({"deep":{"more":[]}}), 100, 2),
        Err(Error::Depth)
    );
}
