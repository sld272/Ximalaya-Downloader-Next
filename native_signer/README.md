# native-signer

APK 协议后端的 Java sidecar 源码。通过 Unidbg 仿真 ARM64 执行喜马拉雅 APK 的四个
native 库，向 Python 侧提供 JSON Lines over stdin/stdout 的 RPC。

## 构建

```bash
cd native_signer
mvn -q package
cp target/native-signer.jar ../vendor/apk_protocol/native-signer.jar
```

要求 JDK 17+ 与 Maven 3.8+。`maven-shade-plugin` 会产出包含全部依赖的 fat JAR
（约 32 MiB，绝大部分是 unidbg/unicorn 各平台的 native 二进制）。

## 依赖与许可

| 依赖 | 版本 | 许可 |
|---|---|---|
| `com.github.zhkl0228:unidbg-android` | 0.9.9 | Apache-2.0 |
| `com.github.zhkl0228:unidbg-unicorn2` | 0.9.9 | Apache-2.0 |
| `com.google.code.gson` | 2.13.1 | Apache-2.0 |
| `org.slf4j:slf4j-nop` | 2.0.17 | MIT |

仓库内随附的 `vendor/apk_protocol/native-signer.jar` 只包含上述开源依赖，
不含任何喜马拉雅代码。

## 重建后需要更新校验清单

`vendor/apk_protocol/manifest.json` 固定了每个资产的 SHA-256，启动时逐项校验。
shade JAR 含构建时间戳，不可逐字节复现，因此**自行重建 JAR 后必须同步更新**
manifest 中的 `native-signer.jar` 哈希，否则启动会以 `ConfigError` 快速失败：

```bash
python3 - <<'PY'
import hashlib, json, pathlib
root = pathlib.Path("vendor/apk_protocol")
manifest = json.loads((root / "manifest.json").read_text())
manifest["files"]["native-signer.jar"] = hashlib.sha256(
    (root / "native-signer.jar").read_bytes()
).hexdigest()
(root / "manifest.json").write_text(
    json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
)
PY
```

## RPC 协议

每行一个 JSON 请求，每行一个 JSON 响应。响应恒定包含 `ok` 字段；`ok=false` 时
`error` 为脱敏后的原因。

| op | 入参 | 出参 |
|---|---|---|
| `ping` | — | — |
| `encryptMobile` | `mobile` | 密文 |
| `sign` | `values`、`production` | 签名 |
| `createXuid` | `stableId` | XUID |
| `ticket` | `attr`、`xuid` | `x-tk` |
| `decryptDownload` | `value`、`version` | 明文 URL |
