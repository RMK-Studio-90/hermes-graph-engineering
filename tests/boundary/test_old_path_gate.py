"""OLD_PATH_REFERENCES = 0: nothing public refers to the retired project location or name."""
import boundary_check

MARKERS = ("graph-engineering-" + "core", "graph_engineering_" + "core")


def test_public_tree_has_no_old_project_references(repo_root, policy):
    hits = []
    for path, rel in boundary_check.iter_public_files(repo_root, policy):
        text = path.read_text(encoding="utf-8", errors="replace")
        hits += [rel for marker in MARKERS if marker in rel or marker in text]
    assert hits == []


def test_old_path_gate_negative_control(tmp_path, policy):
    (tmp_path / "notes.md").write_text("moved from " + MARKERS[0] + "\n", encoding="utf-8")
    found = [rel for path, rel in boundary_check.iter_public_files(tmp_path, policy)
             if MARKERS[0] in path.read_text(encoding="utf-8")]
    assert found == ["notes.md"]
