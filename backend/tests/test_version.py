import json

from conftest import library, run, signed_in
from opus_core import revision


def test_whoever_is_signed_in_is_told_which_build_runs(clean, monkeypatch, tmp_path):
    stamp = tmp_path / "revision.json"
    stamp.write_text(json.dumps({"version": "0.1.651", "sha": "2ae99f9e"}))
    monkeypatch.setattr(revision, "STAMP", stamp)

    async def scenario():
        answers = {}
        for role in ("admin", "user", "guest"):
            async with library(await signed_in(role, role)) as client:
                answers[role] = await client.get("/api/version")
        async with library() as client:
            answers["nobody"] = await client.get("/api/version")
        return answers

    answers = run(scenario())
    for role in ("admin", "user"):
        assert answers[role].status_code == 200, role
        assert answers[role].json() == {"product": "opus-library", "version": "0.1.651", "sha": "2ae99f9e"}
    assert answers["guest"].status_code == 403
    assert answers["nobody"].status_code == 401
