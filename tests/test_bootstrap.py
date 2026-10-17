"""Tests for chat._copy_missing_files — the upgrade step that seeds new wheel
files from _bcoder_data/ into existing ~/.1bcoder/ folders."""
import chat


def test_copies_new_files_into_existing_folders(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "rubrics").mkdir(parents=True)
    (src / "flows" / "__pycache__").mkdir(parents=True)
    (src / "rubrics" / "old.yaml").write_text("wheel", encoding="utf-8")
    (src / "rubrics" / "new.yaml").write_text("wheel", encoding="utf-8")
    (src / "flows" / "ontology.py").write_text("x = 1", encoding="utf-8")
    (src / "flows" / "__pycache__" / "ontology.cpython-312.pyc").write_bytes(b"\0")
    (dst / "rubrics").mkdir(parents=True)
    (dst / "rubrics" / "old.yaml").write_text("user edit", encoding="utf-8")

    copied = chat._copy_missing_files(str(src), str(dst))

    assert sorted(copied) == ["flows/ontology.py", "rubrics/new.yaml"]
    assert (dst / "rubrics" / "old.yaml").read_text(encoding="utf-8") == "user edit"
    assert (dst / "rubrics" / "new.yaml").read_text(encoding="utf-8") == "wheel"
    assert not (dst / "flows" / "__pycache__").exists()
    assert chat._copy_missing_files(str(src), str(dst)) == []
