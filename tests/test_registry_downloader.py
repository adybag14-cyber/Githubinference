from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from githubinference.downloader import ModelDownloadError, download_model, model_url
from githubinference.registry import ModelRegistry, ModelSpec, _validate_spec
from githubinference.scout import scout_models
from githubinference.server import llama_server_command


class _Response(io.BytesIO):
    def __init__(self, content: bytes, status: int) -> None:
        super().__init__(content)
        self.status = status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _small_spec(content: bytes) -> ModelSpec:
    return ModelSpec(
        model_id="test_model",
        display_name="Test model",
        kind="chat",
        repository="owner/model",
        revision="a" * 40,
        filename="model.gguf",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        license="test",
        publisher_trust="upstream",
        automatic_eligible=True,
        context_size=1024,
    )


class RegistryDownloaderTests(unittest.TestCase):
    def test_checked_in_registry_is_fully_pinned(self) -> None:
        registry = ModelRegistry.load()
        self.assertEqual(registry.default_model, "lfm2_5_2_6b_q4_k_m")
        for spec in registry.models.values():
            self.assertEqual(len(spec.revision), 40)
            self.assertEqual(len(spec.sha256), 64)
            self.assertGreater(spec.size_bytes, 0)

    def test_model_url_contains_immutable_revision(self) -> None:
        spec = _small_spec(b"abcdef")
        url = model_url(spec)
        self.assertIn(spec.revision, url)
        self.assertTrue(url.startswith("https://huggingface.co/owner/model/resolve/"))

    def test_resumes_and_verifies_partial_download(self) -> None:
        content = b"abcdef"
        spec = _small_spec(content)
        requests = []

        def urlopen(request: object, timeout: int) -> _Response:
            del timeout
            requests.append(request)
            return _Response(content[3:], 206)

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            partial = directory / f"{spec.cache_filename}.part"
            partial.write_bytes(content[:3])
            result = download_model(spec, directory, retries=1, urlopen=urlopen)
            self.assertEqual(result.read_bytes(), content)
            self.assertFalse(partial.exists())
        self.assertEqual(requests[0].get_header("Range"), "bytes=3-")

    def test_download_stops_after_pinned_size(self) -> None:
        spec = _small_spec(b"abcdef")

        def urlopen(request: object, timeout: int) -> _Response:
            del request, timeout
            return _Response(b"abcdef-plus-unbounded-data", 200)

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ModelDownloadError, "pinned artifact size"):
                download_model(spec, temporary, retries=1, urlopen=urlopen)

    def test_server_command_accepts_only_matching_mtp_assistant(self) -> None:
        registry = ModelRegistry.load()
        target = registry.get("gemma4_e2b_it_qat_q4_0")
        draft = registry.get("gemma4_e2b_it_qat_mtp_q8_0")
        command = llama_server_command(
            "/opt/githubinference/llama-server",
            target,
            "/opt/githubinference/target.gguf",
            draft_spec=draft,
            draft_path="/opt/githubinference/draft.gguf",
        )
        self.assertIn("draft-mtp", command)
        self.assertIn("--spec-draft-model", command)
        self.assertEqual(command[command.index("--reasoning") + 1], "off")
        self.assertEqual(command[command.index("--reasoning-budget") + 1], "0")
        with self.assertRaisesRegex(ValueError, "both draft"):
            llama_server_command(
                "/opt/githubinference/llama-server",
                target,
                "/opt/githubinference/target.gguf",
                draft_spec=draft,
            )

    def test_sampling_parameters_reject_booleans_and_invalid_numbers(self) -> None:
        spec = _small_spec(b"abcdef")
        for invalid in (
            replace(spec, temperature=True),
            replace(spec, top_p="0.5"),  # type: ignore[arg-type]
            replace(spec, top_k=False),
            replace(spec, repeat_penalty=0),
            replace(spec, repeat_penalty=11),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "invalid"):
                    _validate_spec(invalid)

    def test_scout_rejects_missing_required_configuration_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "scout.json"
            config.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing keys"):
                scout_models(config)


if __name__ == "__main__":
    unittest.main()
