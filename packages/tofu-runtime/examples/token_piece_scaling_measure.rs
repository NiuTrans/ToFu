//! Long-piece ordinary BPE scaling, exact full-encoder oracle outside timing.
use serde_json::json;
use std::time::Instant;
use tofu_runtime::token_ingress::{Bounds,Counter,Encoder,Encoding};
fn main() {
    for (name,encoding,full) in [("cl100k_base",Encoding::Cl100k,tiktoken_rs::cl100k_base().unwrap()),
                                ("o200k_base",Encoding::O200k,tiktoken_rs::o200k_base().unwrap())] {
        let counter=Counter::new(7,"task".into(),"fixture".into(),Encoder::new(encoding),
            Bounds {input_bytes:1<<20,text_bytes:1<<20,tool_json_bytes:1<<20,depth:128}).unwrap();
        let template=json!({"model":"fixture"}).as_object().unwrap().clone();
        for (shape,atom) in [("repeated","a"),("alternating","abcdef")] {
            for bytes in [64,256,1024,4096,16384] {
                let text=atom.repeat(bytes/atom.len()+1)[..bytes].to_owned();
                let expected=full.encode_ordinary(&text).len() as u64+
                    tofu_runtime::execution_contract::TOKEN_MESSAGE_OVERHEAD+
                    tofu_runtime::execution_contract::TOKEN_REQUEST_OVERHEAD;
                let messages=[json!({"role":"user","content":text})];
                assert_eq!(counter.count(7,"task",&messages,&template).unwrap(),expected);
                let start=Instant::now();
                for _ in 0..3 {assert_eq!(counter.count(7,"task",&messages,&template).unwrap(),expected);}
                println!("{}",json!({"encoding":name,"shape":shape,"bytes":bytes,"calls":3,"wall_ns":start.elapsed().as_nanos(),"tokens":expected}));
            }
        }
    }
}
