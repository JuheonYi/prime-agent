//! Opt-in copies made after the original state save. Failures are host diagnostics.

use std::path::Path;

use anyhow::Result;
use serde_json::json;

use super::{HarnessScope, HarnessState};
use crate::settings::storage::atomic_write;

#[cfg(test)]
mod tests;

#[derive(Clone)]
pub(crate) struct SnapshotContext {
    pub source: &'static str,
    pub scope: HarnessScope,
    pub session_id: Option<String>,
    pub refinement_id: Option<String>,
}

impl SnapshotContext {
    pub fn host(scope: HarnessScope) -> Self {
        Self {
            source: "host",
            scope,
            session_id: None,
            refinement_id: None,
        }
    }
}

pub(crate) fn record(state_path: &Path, state: &HarnessState, context: &SnapshotContext) {
    if std::env::var("PRIME_AGENT_HARNESS_SNAPSHOTS").as_deref() != Ok("1") {
        return;
    }
    if let Err(error) = write_snapshot(state_path, state, context) {
        eprintln!("harness snapshot error: {}: {error}", state_path.display());
    }
}

fn write_snapshot(
    state_path: &Path,
    state: &HarnessState,
    context: &SnapshotContext,
) -> Result<()> {
    let directory = state_path.with_file_name("snapshots");
    std::fs::create_dir_all(&directory)?;
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)?
        .as_nanos();
    let id = uuid::Uuid::new_v4().simple();
    let path = directory.join(format!("{nanos:020}-{id}.json"));
    let payload = json!({
        "schema": 2,
        "timestamp": crate::session::manager::format_iso_now(),
        "source": context.source,
        "session_id": context.session_id,
        "scope": if context.source == "host" { None } else { Some(context.scope) },
        "refinement_id": context.refinement_id,
        "state": state,
    });
    atomic_write(&path, &serde_json::to_string_pretty(&payload)?)?;
    Ok(())
}
