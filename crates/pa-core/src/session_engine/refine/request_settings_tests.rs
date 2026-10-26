use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use pa_ai::types::{Context, Model, SimpleStreamOptions, StreamOptions};
use pa_ai::{AssistantMessageEventStream, Provider};
use pa_types::ai::ModelThinkingLevel;
use serde_json::json;

use super::{AgentSession, RefineOptions, RefinementSource};

#[derive(Debug, PartialEq)]
struct RequestSettings {
    key: Option<String>,
    headers: Option<BTreeMap<String, String>>,
    reasoning: Option<ModelThinkingLevel>,
}

struct CaptureProvider {
    inner: Arc<dyn Provider>,
    captured: Arc<Mutex<Vec<RequestSettings>>>,
    contexts: Arc<Mutex<Vec<Context>>>,
}

impl Provider for CaptureProvider {
    fn api(&self) -> &str {
        self.inner.api()
    }

    fn stream(
        &self,
        model: &Model,
        context: &Context,
        options: Option<&StreamOptions>,
    ) -> AssistantMessageEventStream {
        self.inner.stream(model, context, options)
    }

    fn stream_simple(
        &self,
        model: &Model,
        context: &Context,
        options: Option<&SimpleStreamOptions>,
    ) -> AssistantMessageEventStream {
        let options = options.expect("refiner supplies request options");
        self.contexts.lock().unwrap().push(context.clone());
        self.captured.lock().unwrap().push(RequestSettings {
            key: options.base.api_key.clone(),
            headers: options
                .base
                .headers
                .clone()
                .map(|headers| headers.into_iter().collect()),
            reasoning: options.reasoning,
        });
        self.inner.stream_simple(model, context, Some(options))
    }
}

#[tokio::test]
async fn refinement_and_auto_review_keep_original_reasoning_with_configured_headers() {
    let api = format!("refiner-settings-{}", uuid::Uuid::new_v4());
    let registration =
        pa_ai::faux::register_faux_provider(pa_ai::faux::RegisterFauxProviderOptions {
            api: Some(api.clone()),
            provider: Some(api.clone()),
            models: Some(vec![pa_ai::faux::FauxModelDefinition {
                id: "test-model".to_string(),
                reasoning: Some(true),
                context_window: Some(100_000),
                max_tokens: Some(8_000),
                ..Default::default()
            }]),
            ..Default::default()
        });
    registration.set_responses(
        [
            r#"{"summary":"test refinement","edits":[]}"#,
            r#"{"shouldRefine":true,"rationale":"useful evidence"}"#,
            r#"{"shouldRefine":true,"rationale":"interval evidence"}"#,
            r#"{"summary":"interval refinement","edits":[]}"#,
        ]
        .into_iter()
        .map(|text| {
            pa_ai::faux::FauxResponseStep::Message(pa_ai::faux::faux_assistant_text_message(
                text,
                pa_ai::faux::FauxAssistantMessageOptions::default(),
            ))
        })
        .collect(),
    );
    let captured = Arc::new(Mutex::new(Vec::new()));
    let contexts = Arc::new(Mutex::new(Vec::new()));
    pa_ai::registry::register_api_provider(
        Arc::new(CaptureProvider {
            inner: pa_ai::registry::get_api_provider(&api).unwrap(),
            captured: captured.clone(),
            contexts: contexts.clone(),
        }),
        Some(&api),
    );
    let dir = tempfile::tempdir().unwrap();
    std::fs::write(
        dir.path().join("models.json"),
        json!({
            "providers": { &api: {
                "apiKey": "fresh-fixture-key",
                "headers": {"X-Configured": "copilot-client"}
            }}
        })
        .to_string(),
    )
    .unwrap();
    let mut model = registration.get_model();
    model.headers = Some(BTreeMap::from([(
        "X-Model".to_string(),
        "base".to_string(),
    )]));
    let agent = Arc::new(pa_agent::agent::Agent::new(
        pa_agent::agent::AgentOptions::default(),
    ));
    agent
        .set_thinking_level(pa_agent::types::ThinkingLevel::High)
        .await;
    let manager = crate::session::manager::SessionManager::persisted(
        dir.path(),
        &dir.path().join("sessions"),
    );
    let mut session = AgentSession::new(agent.clone(), manager, vec![])
        .await
        .unwrap();
    session.set_auxiliary_model_context(
        crate::session_engine::auxiliary_model::AuxiliaryModelContext {
            cwd: dir.path().to_path_buf(),
            agent_dir: dir.path().to_path_buf(),
        },
    );
    session
        .refine(
            &RefineOptions::default(),
            RefinementSource::SelfRefine,
            &model,
            Some("old-session-key".to_string()),
            dir.path().join("harness"),
        )
        .await
        .unwrap();
    agent
        .set_thinking_level(pa_agent::types::ThinkingLevel::Low)
        .await;
    let review = session
        .review_compact_auto_refine(
            &model,
            Some("old-session-key".to_string()),
            &dir.path().join("harness"),
            1,
            0,
        )
        .await
        .unwrap();
    assert!(review.is_some());
    session.set_auto_refine(
        true,
        super::AutoRefineGates {
            turn_interval: 1,
            cooldown_ms: 0,
            compact: false,
            ..Default::default()
        },
    );
    session.note_settled_turn_since_auto_refine_review();
    assert!(session
        .consume_compact_auto_refine(
            &model,
            Some("old-session-key".to_string()),
            dir.path().join("harness"),
            crate::session_engine::auto_refine_trigger::CompactAutoRefineSurface::Checkpoint,
        )
        .await
        .unwrap()
        .is_some());
    let requests = contexts.lock().unwrap();
    let review = json!(requests[2]);
    assert!(review["messages"][0]["content"].as_str().unwrap().contains(
        "<trigger>\nturn_interval; 1 assistant turns since last auto-refine review\n</trigger>"
    ));
    let plan = json!(requests[3]);
    assert!(plan["messages"][0]["content"]
        .as_str()
        .unwrap()
        .contains("turn_interval"));
    let expected: Vec<_> = [pa_types::ai::minimum_reasoning(&model); 4]
        .into_iter()
        .map(|reasoning| RequestSettings {
            key: Some("old-session-key".to_string()),
            headers: Some(BTreeMap::from([
                ("X-Configured".to_string(), "copilot-client".to_string()),
                ("X-Model".to_string(), "base".to_string()),
            ])),
            reasoning,
        })
        .collect();
    assert_eq!(*captured.lock().unwrap(), expected);
    pa_ai::registry::unregister_api_providers(&api);
}
