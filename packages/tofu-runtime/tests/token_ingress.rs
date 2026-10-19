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

#[test]
fn count_only_rank_kernel_matches_full_encoder_on_unicode_repetition_and_concurrency() {
    use tofu_runtime::execution_contract::{TOKEN_MESSAGE_OVERHEAD,TOKEN_REQUEST_OVERHEAD};
    let alphabet=['a','Z','é','中','😀','\n','\r','\t',' ','\0','1','9','_','\u{301}','\u{200d}','\u{10ffff}'];
    let mut state=0x91ab_02cd_ef12_7890u64;
    let mut texts=vec![String::new(),"<|endoftext|><|fim_prefix|>".into(),"123456789".repeat(300)," hello\n".repeat(1000),"a".repeat(4096)];
    for _ in 0..3000 {
        state=state.wrapping_mul(6364136223846793005).wrapping_add(1);
        let length=(state%192) as usize;
        let mut text=String::new();
        for _ in 0..length {
            state=state.wrapping_mul(6364136223846793005).wrapping_add(1);
            text.push(alphabet[(state>>32) as usize%alphabet.len()]);
        }
        texts.push(text);
    }
    for (encoding,full) in [(Encoding::Cl100k,tiktoken_rs::cl100k_base().unwrap()),
                            (Encoding::O200k,tiktoken_rs::o200k_base().unwrap())] {
        let encoder=Encoder::new(encoding);
        let counter=Counter::new(7,"task".into(),"fixture".into(),encoder.clone(),bounds()).unwrap();
        let template=json!({"model":"fixture"}).as_object().unwrap().clone();
        for text in &texts {
            let expected=full.encode_ordinary(text).len() as u64+TOKEN_MESSAGE_OVERHEAD+TOKEN_REQUEST_OVERHEAD;
            assert_eq!(counter.count(7,"task",&[json!({"role":"user","content":text})],&template).unwrap(),expected,"{text:?}");
        }
        std::thread::scope(|scope| {
            for text in texts.iter().take(32) {
                let (counter,template,full)=(&counter,&template,&full);
                scope.spawn(move || {
                    let expected=full.encode_ordinary(text).len() as u64+TOKEN_MESSAGE_OVERHEAD+TOKEN_REQUEST_OVERHEAD;
                    assert_eq!(counter.count(7,"task",&[json!({"role":"user","content":text})],template).unwrap(),expected);
                });
            }
        });
        drop(counter);assert_eq!(std::sync::Arc::strong_count(&encoder),1);
    }
}

#[test]
fn heterogeneous_request_fields_match_full_encoder_across_merge_crossover() {
    use tofu_runtime::execution_contract::{TOKEN_MESSAGE_OVERHEAD, TOKEN_REQUEST_OVERHEAD};
    let texts = [
        String::new(), "x".into(), " qzxvjk".repeat(64),
        "a".repeat(1023), "short again".into(), "a".repeat(1024),
        "abcdef".repeat(1024), "界😀 café\n".repeat(64),
        "one final short field".into(), String::new(),
    ];
    let template = json!({"model":"fixture"}).as_object().unwrap().clone();
    let messages: Vec<_> = texts.iter().map(|text| json!({"role":"user","content":text})).collect();
    for (encoding, full) in [(Encoding::Cl100k, tiktoken_rs::cl100k_base().unwrap()),
                             (Encoding::O200k, tiktoken_rs::o200k_base().unwrap())] {
        let counter = Counter::new(7, "task".into(), "fixture".into(), Encoder::new(encoding), bounds()).unwrap();
        let expected = texts.iter().map(|text| full.encode_ordinary(text).len() as u64).sum::<u64>()
            + TOKEN_MESSAGE_OVERHEAD * texts.len() as u64 + TOKEN_REQUEST_OVERHEAD;
        for _ in 0..3 { assert_eq!(counter.count(7, "task", &messages, &template).unwrap(), expected); }
    }
}
