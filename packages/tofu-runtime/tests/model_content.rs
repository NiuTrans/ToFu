//! Canonical content admission borrows payloads; extra/unknown blocks fail closed.
use serde_json::json;
use tofu_runtime::model_content::validate;
#[test]
fn text_and_image_order_are_preserved_with_no_mutation(){
    let value=json!([{"type":"text","text":"before"},{"type":"image_url","image_url":{"url":"data:image/png;base64,iVBORw==","detail":"high"}},{"type":"text","text":"after"}]);
    let original=value.clone();assert!(validate(&value).is_ok());assert_eq!(value,original);
}
#[test]
fn invalid_or_extra_blocks_refuse(){
    for value in [json!([]),json!([null]),json!([{"type":"text","text":7}]),json!([{"type":"text","text":"ok","extra":true}]),json!([{"type":"image_url","image_url":{"url":""}}]),json!([{"type":"image_url","image_url":{"url":"uri","detail":"original"}}]),json!([{"type":"image_url","image_url":{"url":"uri","unexpected":"extra"}}]),json!([{"type":"audio","data":"secret"}])]{assert!(validate(&value).is_err(),"{value}");}
}
