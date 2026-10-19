//! Disposable loopback probe for the vendored connector's TLS/proxy/H2 contract.
//! Fixture-only CA and policy arrive on stdin; no application or routing state.
use serde_json::{json, Value};
use std::{io::Read, time::Duration};

fn main() {
    let mut input = String::new();
    std::io::stdin()
        .take(1 << 20)
        .read_to_string(&mut input)
        .unwrap();
    let config: Value = serde_json::from_str(&input).unwrap();
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    runtime.block_on(async {
        let mut builder = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .connect_timeout(Duration::from_secs(3))
            .write_timeout(Duration::from_millis(config["write_ms"].as_u64().unwrap()));
        if let Some(pem) = config["ca_pem"].as_str() {
            builder = builder
                .add_root_certificate(reqwest::Certificate::from_pem(pem.as_bytes()).unwrap());
        }
        if let Some(proxy) = config["proxy"].as_str() {
            builder = builder.proxy(reqwest::Proxy::all(proxy).unwrap());
        }
        let client = builder.build().unwrap();
        let mut rows = Vec::new();
        for index in 0..config["requests"].as_u64().unwrap() {
            let recovery = if index > 0 {
                config["recovery_endpoint"].as_str()
            } else {
                None
            };
            let operation = async {
                let response = client
                    .post(recovery.unwrap_or(config["endpoint"].as_str().unwrap()))
                    .body(vec![
                        b'x';
                        if recovery.is_some() {
                            2
                        } else {
                            config["body_bytes"].as_u64().unwrap() as usize
                        }
                    ])
                    .send()
                    .await?;
                let version = format!("{:?}", response.version());
                let text = response.text().await?;
                Ok::<_, reqwest::Error>(json!({"status":"done", "version":version, "text":text}))
            };
            let row = match tokio::time::timeout(
                Duration::from_millis(config["deadline_ms"].as_u64().unwrap()),
                operation,
            )
            .await
            {
                Err(_) => json!({"status":"owner_deadline"}),
                Ok(Ok(value)) => value,
                Ok(Err(error)) => {
                    use std::error::Error;
                    let mut source = Some(&error as &dyn Error);
                    while let Some(item) = source {
                        eprintln!("transport fixture error: {item:?}");
                        source = item.source();
                    }
                    json!({"status":"transport_error", "write_timeout":error.is_write_timeout(),
                        "timeout":error.is_timeout(), "connect":error.is_connect()})
                }
            };
            let failed = row["status"] != "done";
            rows.push(row);
            if failed && config["recovery_endpoint"].is_null() {
                break;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        drop(client);
        println!("{}", json!({"requests":rows}));
    });
}
