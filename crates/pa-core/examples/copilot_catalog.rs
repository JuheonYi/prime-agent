//! Query Copilot through pa-ai's native OAuth transport, without a proxy.
//! Usage: `copilot_catalog <output-directory-containing-agent/auth.json>`

use std::path::PathBuf;

use anyhow::{Context, Result};
use pa_ai::oauth::{
    get_github_copilot_base_url, ProviderHttp, ProviderHttpMethod, ProviderHttpRequest,
    ReqwestProviderHttp,
};
use pa_core::auth::{AuthCredential, AuthStorage};
use serde_json::{json, Value};

async fn request_json(
    http: &dyn ProviderHttp,
    url: String,
    headers: Vec<(String, String)>,
) -> Result<Value> {
    let response = http
        .request(
            ProviderHttpRequest {
                method: ProviderHttpMethod::Get,
                url,
                headers,
                body: None,
                follow_redirects: false,
            },
            30_000,
        )
        .await
        .map_err(|_| anyhow::anyhow!("Copilot catalog or identity transport failed"))?;
    anyhow::ensure!(
        response.ok(),
        "Copilot catalog or identity request failed (HTTP {})",
        response.status
    );
    serde_json::from_str(&response.body).context("invalid Copilot catalog or identity JSON")
}

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    anyhow::ensure!(
        args.len() == 2,
        "usage: copilot_catalog <output directory containing agent/auth.json>"
    );
    let output = PathBuf::from(&args[1]);
    let agent_dir = output.join("agent");
    anyhow::ensure!(
        agent_dir.join("auth.json").is_file(),
        "native Copilot login is missing"
    );
    let mut auth = AuthStorage::create(&agent_dir);
    let Some(AuthCredential::Oauth {
        refresh,
        enterprise_url,
        ..
    }) = auth.get_all().credential("github-copilot")
    else {
        anyhow::bail!("native Copilot OAuth login is missing");
    };
    let access = auth
        .get_api_key("github-copilot")
        .context("native Copilot credential could not be resolved or refreshed")?;
    let http = ReqwestProviderHttp::new();
    let github_token = refresh.context("native Copilot login has no GitHub identity token")?;
    let domain = enterprise_url.as_deref().unwrap_or("github.com");
    let identity = request_json(
        &http,
        format!("https://api.{domain}/user"),
        vec![
            (
                "Authorization".to_string(),
                format!("Bearer {github_token}"),
            ),
            ("User-Agent".to_string(), "Prime-Agent".to_string()),
            ("Accept".to_string(), "application/json".to_string()),
        ],
    )
    .await?;
    let account = identity
        .get("login")
        .and_then(Value::as_str)
        .context("GitHub login not returned")?;
    let endpoint = get_github_copilot_base_url(Some(&access), enterprise_url.as_deref());
    let mut headers: Vec<_> = pa_ai::models_generated::get_models("github-copilot")
        .into_iter()
        .find(|model| model.api == "openai-responses")
        .and_then(|model| model.headers.clone())
        .context("native Copilot request headers are unavailable")?
        .into_iter()
        .collect();
    headers.push(("Authorization".to_string(), format!("Bearer {access}")));
    headers.push(("Accept".to_string(), "application/json".to_string()));
    let document = request_json(
        &http,
        format!("{}/models", endpoint.trim_end_matches('/')),
        headers,
    )
    .await?;
    let rows = document
        .as_array()
        .or_else(|| document.get("data").and_then(Value::as_array))
        .context("Copilot catalog has no model list")?;
    let models: Vec<Value> = rows
        .iter()
        .filter(|model| {
            model
                .get("id")
                .and_then(Value::as_str)
                .is_some_and(|id| id.starts_with("gpt-"))
        })
        .map(|model| {
            json!({
                "id": model["id"],
                "name": model["name"],
                "capabilities": model["capabilities"],
                "supported_endpoints": model["supported_endpoints"],
                "policy": model["policy"],
                "model_picker_enabled": model["model_picker_enabled"],
                "billing": model["billing"],
            })
        })
        .collect();
    std::fs::create_dir_all(&output)?;
    let catalog = output.join("gpt-models.json");
    std::fs::write(
        &catalog,
        serde_json::to_string_pretty(&json!({
            "account": account,
            "endpoint": endpoint,
            "queried_at": pa_core::session::manager::format_iso_now(),
            "models": models,
        }))?,
    )?;
    println!(
        "{} GPT models available for {account}; catalog: {}",
        models.len(),
        catalog.display(),
    );
    Ok(())
}
