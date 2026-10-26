//! Bounded live transport/refinement check; never executes model-generated code.
//! Usage: `copilot_smoke <agent-dir> <new-output-dir>`

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context as _, Result};
use pa_ai::types::{SimpleStreamOptions, StreamOptions};
use pa_core::auth::AuthStorage;
use pa_core::models::ModelRegistry;
use pa_core::session::manager::SessionManager;
use pa_core::session_engine::auxiliary_model::AuxiliaryModelContext;
use pa_core::session_engine::refine::{RefineOptions, RefinementSource};
use pa_core::session_engine::AgentSession;
use pa_types::ai::{
    AssistantContentBlock, AssistantMessage, Context, Message, Model, ModelThinkingLevel,
    StopReason, Tool, UserContent, UserMessage,
};
use pa_types::session::AgentMessage;
use serde_json::{json, Value};

fn write_json(path: &Path, value: &impl serde::Serialize) -> Result<()> {
    std::fs::write(path, serde_json::to_string_pretty(value)?)?;
    Ok(())
}

async fn request(
    model: &Model,
    context: &Context,
    options: SimpleStreamOptions,
    output: &Path,
) -> Result<AssistantMessage> {
    let reply = tokio::time::timeout(
        Duration::from_secs(180),
        pa_ai::complete_simple(model, context, Some(options)),
    )
    .await
    .context("Copilot probe timed out")??;
    write_json(output, &reply)?;
    anyhow::ensure!(
        matches!(reply.stop_reason, StopReason::Stop | StopReason::ToolUse),
        "Copilot probe failed or exhausted its output budget; see {}",
        output.display()
    );
    Ok(reply)
}

async fn tool_roundtrip(
    model: &Model,
    options: &SimpleStreamOptions,
    output: &Path,
) -> Result<Vec<Message>> {
    let mut context = Context {
        system_prompt: Some("This is a connection check. Call add_numbers once with a=19 and b=23. After the tool returns, reply with exactly its numeric result.".to_string()),
        messages: vec![Message::User(UserMessage {
            content: UserContent::Text("Compute 19 + 23 using the supplied tool.".to_string()),
            timestamp: 0,
            rest: serde_json::Map::default(),
        })],
        tools: Some(vec![Tool {
            name: "add_numbers".to_string(),
            description: "Return the sum of the two integers.".to_string(),
            parameters: json!({
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"], "additionalProperties": false
            }),
        }]),
    };
    let first = request(
        model,
        &context,
        options.clone(),
        &output.join("tool-call.json"),
    )
    .await?;
    let calls: Vec<_> = first
        .content
        .iter()
        .filter_map(|block| match block {
            AssistantContentBlock::ToolCall(call) => Some(call),
            _ => None,
        })
        .collect();
    anyhow::ensure!(calls.len() == 1, "expected exactly one tool call");
    let call = calls[0];
    anyhow::ensure!(call.name == "add_numbers", "unexpected tool requested");
    anyhow::ensure!(
        call.arguments.get("a") == Some(&json!(19)) && call.arguments.get("b") == Some(&json!(23)),
        "incorrect tool arguments"
    );
    let result: pa_types::ai::ToolResultMessage = serde_json::from_value(json!({
        "toolCallId": call.id, "toolName": call.name,
        "content": [{"type": "text", "text": "42"}], "isError": false, "timestamp": 0
    }))?;
    context.messages.push(Message::Assistant(first));
    context.messages.push(Message::ToolResult(result));
    let second = request(
        model,
        &context,
        options.clone(),
        &output.join("tool-return.json"),
    )
    .await?;
    let text: String = second
        .content
        .iter()
        .filter_map(|block| match block {
            AssistantContentBlock::Text(text) => Some(text.text.as_str()),
            _ => None,
        })
        .collect();
    anyhow::ensure!(
        second.stop_reason == StopReason::Stop && text.trim() == "42",
        "tool result was not consumed correctly"
    );
    context.messages.push(Message::Assistant(second));
    Ok(context.messages)
}

async fn run(agent_dir: &Path, output: &Path) -> Result<Value> {
    let auth = AuthStorage::create(agent_dir);
    let mut registry = ModelRegistry::create(auth, agent_dir.join("models.json"));
    let mut model = registry
        .get_all()
        .iter()
        .find(|model| model.provider == "github-copilot" && model.id == "gpt-5.6-sol")
        .context("configured GPT-5.6 Sol model not found")?
        .clone();
    model.max_tokens = 4096;
    model.max_tokens_explicit = true;
    let resolved = registry.get_api_key_and_headers(&model, model.headers.as_ref());
    anyhow::ensure!(
        resolved.ok && resolved.api_key.is_some(),
        "native Copilot authentication unavailable"
    );
    anyhow::ensure!(
        model.base_url.starts_with("https://api.")
            && model.base_url.ends_with(".githubcopilot.com"),
        "probe must use native Copilot, not a proxy"
    );
    let options = SimpleStreamOptions {
        base: StreamOptions {
            api_key: resolved.api_key.clone(),
            headers: resolved
                .headers
                .map(|headers| headers.into_iter().collect()),
            max_tokens: Some(4096),
            timeout_ms: Some(180_000),
            ..Default::default()
        },
        reasoning: Some(ModelThinkingLevel::Medium),
        ..Default::default()
    };
    let messages = tool_roundtrip(&model, &options, output).await?;
    let mut manager = SessionManager::persisted(output, &output.join("sessions"));
    for message in messages {
        manager.append_message(match message {
            Message::User(message) => AgentMessage::User(message),
            Message::Assistant(message) => AgentMessage::Assistant(message),
            Message::ToolResult(message) => AgentMessage::ToolResult(message),
        })?;
    }
    let session_id = manager.get_session_id().to_string();
    let agent = Arc::new(pa_agent::agent::Agent::new(
        pa_agent::agent::AgentOptions::default(),
    ));
    agent
        .set_thinking_level(pa_agent::types::ThinkingLevel::Medium)
        .await;
    let mut session = AgentSession::new(agent, manager, vec![]).await?;
    session.set_auxiliary_model_context(AuxiliaryModelContext {
        cwd: output.to_path_buf(),
        agent_dir: agent_dir.to_path_buf(),
    });
    let refined = tokio::time::timeout(Duration::from_secs(180), session.refine(
        &RefineOptions {
            instructions: Some("Connection-check fixture, not benchmark learning: create exactly one local memory with id tool-contract and title Add tool contract. Record that add_numbers accepts integer a and b and returns their sum, as demonstrated in the trajectory. Do not create other entries.".to_string()),
            ..Default::default()
        },
        RefinementSource::User, &model, resolved.api_key, agent_dir.join("harness"),
    )).await.context("Copilot refinement timed out")??;
    write_json(&output.join("refinement.json"), &refined)?;
    anyhow::ensure!(
        refined.applied_edits.len() == 1 && refined.applied_edits[0].applied,
        "refinement edit did not apply"
    );
    let path = PathBuf::from(&refined.harness_state_path);
    let state: Value = serde_json::from_str(&std::fs::read_to_string(&path)?)?;
    anyhow::ensure!(
        state["entries"]["memory"]["tool-contract"].is_object(),
        "refinement memory missing"
    );
    let snapshot_directory = path
        .parent()
        .context("harness directory missing")?
        .join("snapshots");
    Ok(json!({
        "status": "passed", "model": model.id, "endpoint": model.base_url,
        "main_reasoning": "medium", "refiner_reasoning": pa_types::ai::minimum_reasoning(&model),
        "logical_model_requests": 3,
        "max_output_tokens_per_request": 4096, "session_id": session_id,
        "tool_roundtrip": "19 + 23 = 42", "refinement_applied": true,
        "harness_state_path": path, "snapshot_directory": snapshot_directory,
        "benchmark_run": false, "subagent_run": false,
        "scope": "Native inference, tool-call protocol, and session refinement only; snapshot recording is opt-in."
    }))
}

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<_> = std::env::args().collect();
    anyhow::ensure!(
        args.len() == 3,
        "usage: copilot_smoke <agent-dir> <new-output-dir>"
    );
    let agent_dir = PathBuf::from(&args[1]);
    let output = PathBuf::from(&args[2]);
    anyhow::ensure!(
        agent_dir.join("auth.json").is_file(),
        "native login missing"
    );
    std::fs::create_dir(&output).context("output directory must be new")?;
    let result = Box::pin(run(&agent_dir, &output)).await;
    let report = match &result {
        Ok(report) => report.clone(),
        Err(error) => {
            json!({"status": "failed", "error": error.to_string(), "benchmark_run": false})
        }
    };
    write_json(&output.join("result.json"), &report)?;
    result?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}
