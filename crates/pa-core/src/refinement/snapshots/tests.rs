use super::*;
use crate::refinement::{empty_harness_state, save_harness_state};

#[test]
fn snapshots_copy_state_without_changing_it_or_overwriting_history() {
    let dir = tempfile::tempdir().unwrap();
    let state = empty_harness_state();
    let path = save_harness_state(dir.path(), &state).unwrap();
    let original = std::fs::read(&path).unwrap();
    let context = SnapshotContext::host(HarnessScope::Local);
    write_snapshot(&path, &state, &context).unwrap();
    let first = std::fs::read_dir(dir.path().join("snapshots"))
        .unwrap()
        .next()
        .unwrap()
        .unwrap()
        .path();
    let previous = std::fs::read(&first).unwrap();
    write_snapshot(&path, &state, &context).unwrap();
    assert_eq!(std::fs::read(first).unwrap(), previous);
    assert_eq!(std::fs::read(&path).unwrap(), original);
    assert_eq!(
        std::fs::read_dir(dir.path().join("snapshots"))
            .unwrap()
            .count(),
        2
    );
}

#[test]
fn failed_snapshot_write_leaves_committed_state_untouched() {
    let dir = tempfile::tempdir().unwrap();
    let state = empty_harness_state();
    let path = save_harness_state(dir.path(), &state).unwrap();
    let original = std::fs::read(&path).unwrap();
    std::fs::write(dir.path().join("snapshots"), "blocked").unwrap();
    assert!(write_snapshot(&path, &state, &SnapshotContext::host(HarnessScope::Local)).is_err());
    assert_eq!(std::fs::read(&path).unwrap(), original);
}
