"""render_motion_clip / render_still file:// preflight — pure logic, no
Chromium needed. The tool runs platform-side: file:// refs must localize
through _resolve_path, and unresolvable ones must fail LOUDLY before
capture (the historical behavior was a silent broken frame)."""

import pytest

import motion


def _mapped_resolver(monkeypatch, mapping):
    def resolve(p):
        if p in mapping:
            return mapping[p]
        raise ValueError(f"Cannot open '{p}'")
    monkeypatch.setattr(motion, "_resolve_path", resolve)


def test_localize_rewrites_resolvable_refs(monkeypatch):
    _mapped_resolver(monkeypatch,
                     {"/workspace/a.png": "/agents/x/users/u/workspace/a.png"})
    doc = '<img src="file:///workspace/a.png">'
    out, bad = motion._localize_file_urls(doc)
    assert bad == []
    assert "file:///agents/x/users/u/workspace/a.png" in out


def test_localize_percent_decodes_before_resolving(monkeypatch):
    _mapped_resolver(monkeypatch,
                     {"/workspace/my img.png": "/agents/x/my img.png"})
    out, bad = motion._localize_file_urls(
        'url("file:///workspace/my%20img.png")')
    assert bad == []
    assert "file:///agents/x/my%20img.png" in out


def test_localize_collects_unresolvable(monkeypatch):
    _mapped_resolver(monkeypatch, {})
    doc = '<img src="file:///home/user/gone.png">'
    out, bad = motion._localize_file_urls(doc)
    assert bad == ["file:///home/user/gone.png"]
    assert out == doc


def test_prepare_inline_fails_loud_on_bad_ref(monkeypatch, tmp_path):
    _mapped_resolver(monkeypatch, {})
    with pytest.raises(ValueError) as e:
        motion._prepare_page_doc('<img src="file:///nope/x.png">', None,
                                 tmp_path, "m.html")
    msg = str(e.value)
    assert "file:///nope/x.png" in msg
    assert "platform-side" in msg


def test_prepare_htmlpath_without_refs_loads_in_place(monkeypatch, tmp_path):
    src = tmp_path / "doc.html"
    src.write_text("<html><body>hi</body></html>", encoding="utf-8")
    _mapped_resolver(monkeypatch, {str(src): str(src)})
    got = motion._prepare_page_doc(None, str(src), tmp_path, "m.html")
    assert got == str(src)  # relative refs keep their original base


def test_prepare_htmlpath_rewrite_injects_base(monkeypatch, tmp_path):
    src = tmp_path / "doc.html"
    src.write_text('<html><head><title>t</title></head>'
                   '<body><img src="file:///workspace/a.png">'
                   '<img src="rel.png"></body></html>', encoding="utf-8")
    _mapped_resolver(monkeypatch, {
        str(src): str(src),
        "/workspace/a.png": "/agents/x/a.png",
    })
    got = motion._prepare_page_doc(None, str(src), tmp_path, "m.html")
    assert got != str(src)
    text = (tmp_path / "m.html").read_text(encoding="utf-8")
    assert "file:///agents/x/a.png" in text
    base = f'<base href="{src.parent.as_uri()}/">'
    head_end = text.lower().find("<head")
    assert base in text and text.find(base) > head_end
