from opus.photos.library import derive


def test_a_marker_it_can_no_longer_rewrite_does_not_stop_the_store(tmp_path):
    derive.prepare_store(tmp_path)
    tag = tmp_path / "CACHEDIR.TAG"
    assert tag.read_text() == derive.CACHEDIR_TAG
    assert (tmp_path / ".pxarexclude").read_text() == "*\n"

    tag.chmod(0o444)
    (tmp_path / ".pxarexclude").chmod(0o444)
    derive.prepare_store(tmp_path)
    assert (tmp_path / "tile").is_dir()


def test_a_marker_saying_something_else_is_corrected(tmp_path):
    derive.prepare_store(tmp_path)
    (tmp_path / ".pxarexclude").write_text("old\n")
    derive.prepare_store(tmp_path)
    assert (tmp_path / ".pxarexclude").read_text() == "*\n"
