//! Isolated-process replay: snapshot flags never race across parallel tests.
use std::path::Path;
use std::process::Command;
use std::sync::{Arc, Mutex};

use pa_core::refinement::{load_harness_state, HarnessScope};
use pa_core::session::manager::SessionManager;
use pa_core::session_engine::refine::{
    create_refinement_notice_message, execute_refinement, local_harness_state_dir, RefineOptions,
    RefinementSource, RefinementTranscript,
};
use serde_json::{json, Value};

fn normalize(value: &mut Value, ids: &[String]) {
    match value {
        Value::Object(object) => {
            object.remove("created_at");
            object.remove("updated_at");
            for item in object.values_mut() {
                normalize(item, ids);
            }
        }
        Value::Array(items) => {
            for item in items {
                normalize(item, ids);
            }
        }
        Value::String(text) => {
            for id in ids {
                *text = text.replace(id, "<refinement-id>");
            }
        }
        _ => {}
    }
}

#[test]
fn logging_off_on_and_failure_preserve_prompts_results_and_state() {
    let mut reports = Vec::new();
    for mode in ["off", "on", "failure"] {
        let directory = tempfile::tempdir().unwrap();
        let output = Command::new(std::env::current_exe().unwrap())
            .args(["--ignored", "--exact", "replay_fixture", "--nocapture"])
            .env("HARNESS_REPLAY_DIR", directory.path())
            .env("HARNESS_REPLAY_MODE", mode)
            .env(
                "PRIME_AGENT_HARNESS_SNAPSHOTS",
                if mode == "off" { "0" } else { "1" },
            )
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        assert_eq!(
            String::from_utf8_lossy(&output.stderr).contains("harness snapshot error:"),
            mode == "failure"
        );
        let report: Value = serde_json::from_str(
            &std::fs::read_to_string(directory.path().join("report.json")).unwrap(),
        )
        .unwrap();
        reports.push(report);
    }
    assert_eq!(reports[0], reports[1]);
    assert_eq!(reports[0], reports[2]);
}

#[tokio::test]
#[ignore = "invoked by the isolated-process comparison test"]
#[allow(clippy::too_many_lines)] // One continuous replay checks all sources and their rollback.
async fn replay_fixture() {
    let directory = std::env::var("HARNESS_REPLAY_DIR").unwrap();
    let directory = Path::new(&directory);
    let mode = std::env::var("HARNESS_REPLAY_MODE").unwrap();
    let mut session = SessionManager::persisted(directory, &directory.join("sessions"));
    let local = local_harness_state_dir(&session);
    if mode == "failure" {
        std::fs::create_dir_all(&local).unwrap();
        std::fs::write(local.join("snapshots"), "blocked").unwrap();
    }
    let model = serde_json::from_value(json!({
        "id":"fixture", "name":"fixture", "api":"faux", "provider":"faux",
        "baseUrl":"http://localhost:0", "reasoning":false, "input":["text"],
        "contextWindow":100_000, "maxTokens":8000,
        "cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0}
    }))
    .unwrap();
    let prompts = Arc::new(Mutex::new(Vec::new()));
    let mut results = Vec::new();
    let mut ids = Vec::new();
    for (index, source) in [
        RefinementSource::SelfRefine,
        RefinementSource::Auto,
        RefinementSource::User,
    ]
    .into_iter()
    .enumerate()
    {
        let prompts = Arc::clone(&prompts);
        let proposal = json!({"summary":"verified lesson","edits":[{
            "action":if index == 0 {"create"} else {"update"},
            "kind":"memory","id":"lesson","title":"Lesson","content":format!("v{index}")
        }]})
        .to_string();
        let result = execute_refinement(
            &mut session,
            RefinementTranscript {
                messages: &[],
                refinement_history: &[],
            },
            &directory.join("global"),
            &model,
            &RefineOptions::default(),
            source,
            Box::new(move |_model, system, user| {
                Box::pin(async move {
                    prompts
                        .lock()
                        .unwrap()
                        .push(json!({"system":system, "user":user}));
                    Ok(pa_ai::faux::faux_assistant_text_message(
                        &proposal,
                        pa_ai::faux::FauxAssistantMessageOptions::default(),
                    ))
                })
            }),
            None,
        )
        .await
        .unwrap();
        ids.push(result.id.clone());
        results.push(result);
    }
    let rollback = execute_refinement(
        &mut session,
        RefinementTranscript {
            messages: &[],
            refinement_history: &results,
        },
        &directory.join("global"),
        &model,
        &RefineOptions {
            rollback_id: Some(results[2].id.clone()),
            ..Default::default()
        },
        RefinementSource::User,
        Box::new(|_, _, _| panic!("rollback cannot call a model")),
        None,
    )
    .await
    .unwrap();
    ids.push(rollback.id.clone());
    results.push(rollback);
    let state = load_harness_state(&local, HarnessScope::Local);
    let snapshots: Vec<_> = if mode == "on" {
        let mut paths: Vec<_> = std::fs::read_dir(local.join("snapshots"))
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .collect();
        paths.sort();
        paths
            .into_iter()
            .map(|path| {
                serde_json::from_str::<Value>(&std::fs::read_to_string(path).unwrap()).unwrap()
            })
            .collect()
    } else {
        Vec::new()
    };
    if mode == "on" {
        assert_eq!(snapshots.len(), 4);
        assert_eq!(
            snapshots
                .iter()
                .map(|row| row["source"].as_str().unwrap())
                .collect::<Vec<_>>(),
            ["agent_refine", "auto_refine", "user_refine", "rollback"]
        );
        assert_eq!(snapshots[3]["state"], serde_json::to_value(&state).unwrap());
    } else if mode == "off" {
        assert!(!local.join("snapshots").exists());
    }
    let mut report = json!({
        "prompts": *prompts.lock().unwrap(),
        "edits": results.iter().map(|result| &result.applied_edits).collect::<Vec<_>>(),
        "notices": results.iter().map(|result| create_refinement_notice_message(result, RefinementSource::User).content).collect::<Vec<_>>(),
        "state": state,
    });
    normalize(&mut report, &ids);
    std::fs::write(
        directory.join("report.json"),
        serde_json::to_string(&report).unwrap(),
    )
    .unwrap();
}
