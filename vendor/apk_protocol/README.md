# vendor/apk_protocol — APK 协议 native 资产

`source_backend=apk` 运行所需的版本绑定资产包。仅在选择 APK 后端时加载，
`http`／`pc`／`chrome` 后端不会读取本目录任何文件。

## 内容与来源

| 文件 | 大小 | 来源 | 许可 |
|---|---|---|---|
| `native-signer.jar` | ~32 MiB | 由本仓库 `native_signer/` 源码 `mvn package` 构建 | Apache-2.0 / MIT（见 `native_signer/README.md`） |
| `libc++_shared.so` | ~969 KiB | 喜马拉雅 Android APK 9.5.1.3 (arm64-v8a) | 喜马拉雅专有 |
| `libencrypt.so` | ~570 KiB | 喜马拉雅 Android APK 9.5.1.3 (arm64-v8a) | 喜马拉雅专有 |
| `libnativelib.so` | ~439 KiB | 喜马拉雅 Android APK 9.5.1.3 (arm64-v8a) | 喜马拉雅专有 |
| `liblogin_encrypt.so` | ~126 KiB | 喜马拉雅 Android APK 9.5.1.3 (arm64-v8a) | 喜马拉雅专有 |
| `assets/na.czl` | — | 喜马拉雅 Android APK 9.5.1.3 | 喜马拉雅专有 |
| `assets/drawable/x_m.png` | — | 喜马拉雅 Android APK 9.5.1.3 | 喜马拉雅专有 |

标注为「喜马拉雅专有」的文件从官方 APK 原样提取，未经修改，也未反编译或改写，
仅供本项目在本地仿真环境中调用，以便用户下载**其账号已获授权**的内容。
版权归上海喜马拉雅科技有限公司所有，本仓库不主张任何权利。

如果你所在的司法辖区不允许再分发这些文件，请删除本目录下除
`native-signer.jar`、`manifest.json` 与本文件外的内容，自行从本地已授权安装的
APK 中提取同版本资产，再通过 `Settings.apk_*` 字段指向它们。

## 版本绑定与校验

`manifest.json` 记录 APK 版本、ABI、最低 Java 版本与每个文件的 SHA-256。
`ApkNativeBridge.open()` 启动前逐项校验，任何缺失或哈希不符都会抛出 `ConfigError`
快速失败，而不是猜测兼容。升级 APK 版本时必须整包替换并更新 manifest。

自行重建 `native-signer.jar` 后需同步更新 manifest 哈希，步骤见
`native_signer/README.md`。

## 运行要求

- Java 17 或更高版本（`Settings.apk_java_path` 可指定具体路径）
- Windows / macOS / Linux 均由 Unidbg 仿真 ARM64，不要求宿主为 ARM 架构
