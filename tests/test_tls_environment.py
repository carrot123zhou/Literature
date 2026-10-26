"""TLS 信任库环境变量治理（providers.normalize_tls_environment）回归测试。

背景（2026-10-25 真实故障）：冻结包构建 venv 曾使用 Codex 托管 Python，其 stdlib
ssl.py 的 SSLContext.__new__ 会加载同目录 codex-rejected-cas.pem；冻结后该文件缺失，
httpx.Client 构造时 create_default_context 直接 FileNotFoundError，云端解析与模型
请求全部失败。修复分两层：
1. 启动时清理指向不存在路径的 SSL_CERT_FILE / SSL_CERT_DIR / REQUESTS_CA_BUNDLE /
   CURL_CA_BUNDLE（保留存在的自定义 CA，绝不关闭证书校验）；
2. httpx.Client 构造失败时翻译为可操作的中文 ProviderError/MinerUAPIError。

本文件只测环境变量治理与错误翻译本身，不发起真实网络请求。
"""

from __future__ import annotations

import os
import ssl
import tempfile
import unittest
from unittest import mock

import httpx

from literature.providers import OpenAICompatibleProvider, ProviderConfig, normalize_proxy_environment, normalize_tls_environment, tls_context_error


class TlsEnvironmentCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = {name: os.environ.get(name) for name in (
            "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
        )}
        for name in self._saved:
            os.environ.pop(name, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.existing_file = os.path.join(self._tmp.name, "custom-ca.pem")
        with open(self.existing_file, "wb") as handle:
            handle.write(b"-----BEGIN CERTIFICATE-----\nZmFrZQ==\n-----END CERTIFICATE-----\n")
        self.existing_dir = os.path.join(self._tmp.name, "certs")
        os.mkdir(self.existing_dir)

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_nonexistent_ca_file_is_removed_and_reported(self) -> None:
        os.environ["SSL_CERT_FILE"] = "/nonexistent/literature-test/missing.pem"
        removed = normalize_tls_environment()
        self.assertIn("SSL_CERT_FILE", removed)
        self.assertNotIn("SSL_CERT_FILE", os.environ)

    def test_existing_custom_ca_file_is_kept(self) -> None:
        os.environ["SSL_CERT_FILE"] = self.existing_file
        removed = normalize_tls_environment()
        self.assertEqual(removed, [])
        self.assertEqual(os.environ.get("SSL_CERT_FILE"), self.existing_file)

    def test_nonexistent_ca_dir_is_removed_and_existing_dir_kept(self) -> None:
        os.environ["SSL_CERT_DIR"] = "/nonexistent/literature-test/certs"
        self.assertIn("SSL_CERT_DIR", normalize_tls_environment())
        self.assertNotIn("SSL_CERT_DIR", os.environ)
        os.environ["SSL_CERT_DIR"] = self.existing_dir
        self.assertEqual(normalize_tls_environment(), [])
        self.assertEqual(os.environ.get("SSL_CERT_DIR"), self.existing_dir)

    def test_requests_and_curl_bundles_follow_same_rule(self) -> None:
        os.environ["REQUESTS_CA_BUNDLE"] = "/nonexistent/literature-test/requests.pem"
        os.environ["CURL_CA_BUNDLE"] = self.existing_file
        removed = normalize_tls_environment()
        self.assertIn("REQUESTS_CA_BUNDLE", removed)
        self.assertNotIn("CURL_CA_BUNDLE", removed)
        self.assertNotIn("REQUESTS_CA_BUNDLE", os.environ)
        self.assertEqual(os.environ.get("CURL_CA_BUNDLE"), self.existing_file)

    def test_tilde_path_is_expanded_before_checking(self) -> None:
        os.environ["SSL_CERT_FILE"] = "~/literature-definitely-missing/ca.pem"
        removed = normalize_tls_environment()
        self.assertIn("SSL_CERT_FILE", removed)
        self.assertNotIn("SSL_CERT_FILE", os.environ)

    def test_unrelated_environment_untouched(self) -> None:
        os.environ["PATH"] = os.environ.get("PATH", "/usr/bin")
        self.assertEqual(normalize_tls_environment(), [])

    def test_cleanup_restores_default_https_context_creation(self) -> None:
        """真实故障等价复现：坏 CA 路径会让 httpx.Client 构造即崩；清理后恢复。"""
        bad = "/nonexistent/literature-test/missing.pem"
        # 先修 NO_PROXY 的 [::1] 条目（同批修复），否则 httpx 在 URL 解析阶段就 InvalidURL。
        normalize_proxy_environment()
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": bad}):
            with self.assertRaises(OSError):
                httpx.Client()
            os.environ["SSL_CERT_FILE"] = bad
            normalize_tls_environment()
            client = httpx.Client()
            client.close()
        # 证书校验保持开启：默认上下文必须是 CERT_REQUIRED，不得因清理而降级。
        context = ssl.create_default_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)


class TlsErrorTranslationTests(unittest.TestCase):
    def test_tls_context_error_is_actionable_chinese_without_secret(self) -> None:
        message = tls_context_error(FileNotFoundError("[Errno 2] No such file or directory"))
        self.assertIn("TLS", message)
        self.assertIn("SSL_CERT_FILE", message)
        self.assertIn("重启 Literature", message)
        self.assertNotIn("sk-", message)

    def test_provider_client_failure_raises_provider_error_not_raw_oserror(self) -> None:
        """冻结故障路径：构造 httpx.Client 遇到坏信任库时，模型请求路径必须给出
        可操作的 ProviderError，而不是裸 FileNotFoundError。"""
        config = ProviderConfig(provider="openai", base_url="https://api.openai.com/v1", model="gpt-test")
        provider = OpenAICompatibleProvider(config, api_key="test-key")
        bad = "/nonexistent/literature-test/missing.pem"
        with mock.patch.dict(os.environ, {"SSL_CERT_FILE": bad}):
            with self.assertRaises(Exception) as caught:
                provider._client_instance(httpx)
            self.assertNotIsInstance(caught.exception, FileNotFoundError)
            self.assertIn("TLS", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
