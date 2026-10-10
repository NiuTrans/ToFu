//! Strict canonical model-facing text/image blocks; schemas derive from stdio.
//! Validation borrows payloads and never fetches, resolves or serializes media.
use crate::execution_contract::{
    NATIVE_IMAGE_BLOCK_KEYS, NATIVE_IMAGE_DETAILS, NATIVE_IMAGE_SOURCE_KEYS, NATIVE_IMAGE_URL_MIN_CHARS, NATIVE_TEXT_BLOCK_KEYS,
};
use serde_json::Value;
#[derive(Debug, PartialEq, Eq)]
pub struct InvalidContent;
fn keys(value: &serde_json::Map<String, Value>, allowed: &[&str]) -> bool {
    value.keys().all(|key| allowed.contains(&key.as_str()))
}
pub fn validate(value: &Value) -> Result<(), InvalidContent> {
    let blocks = value
        .as_array()
        .filter(|a| !a.is_empty())
        .ok_or(InvalidContent)?;
    for block in blocks {
        let object = block.as_object().ok_or(InvalidContent)?;
        match block["type"].as_str() {
            Some("text") if keys(object, NATIVE_TEXT_BLOCK_KEYS) && block["text"].is_string() => {}
            Some("image_url") if keys(object, NATIVE_IMAGE_BLOCK_KEYS) => {
                let image = block["image_url"].as_object().ok_or(InvalidContent)?;
                if !keys(image, NATIVE_IMAGE_SOURCE_KEYS)
                    || !image
                        .get("url")
                        .and_then(Value::as_str)
                        .is_some_and(|s| s.chars().take(NATIVE_IMAGE_URL_MIN_CHARS).count() >= NATIVE_IMAGE_URL_MIN_CHARS)
                    || image.get("detail").is_some_and(|v| {
                        !v.as_str()
                            .is_some_and(|s| NATIVE_IMAGE_DETAILS.contains(&s))
                    })
                {
                    return Err(InvalidContent);
                }
            }
            _ => return Err(InvalidContent),
        }
    }
    Ok(())
}
