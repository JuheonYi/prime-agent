//! Private pipe output for the isolated experiment gateway, not a user-facing command.
use std::io::IsTerminal;
use std::path::PathBuf;

use anyhow::{Context, Result};
use pa_core::auth::{AuthCredential, AuthStorage};

fn main() -> Result<()> {
    anyhow::ensure!(
        !std::io::stdout().is_terminal(),
        "credential output requires a private pipe"
    );
    let directory = PathBuf::from(
        std::env::args()
            .nth(1)
            .context("agent directory required")?,
    );
    anyhow::ensure!(
        directory.join("auth.json").is_file(),
        "native login missing"
    );
    let mut auth = AuthStorage::create(directory);
    let access = auth
        .get_api_key("github-copilot")
        .context("native credential unavailable")?;
    let Some(AuthCredential::Oauth {
        expires,
        enterprise_url,
        ..
    }) = auth.get_all().credential("github-copilot")
    else {
        anyhow::bail!("native Copilot OAuth required");
    };
    let endpoint =
        pa_ai::oauth::get_github_copilot_base_url(Some(&access), enterprise_url.as_deref());
    println!(
        "{}",
        serde_json::json!({"access": access, "endpoint": endpoint, "expires": expires})
    );
    Ok(())
}
