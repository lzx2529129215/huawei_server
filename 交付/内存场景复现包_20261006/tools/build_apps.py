"""Build and sign eight distinct, application-owned workload HAPs."""
from __future__ import annotations
import argparse
import json
import os
import shutil
import subprocess
import time
import sys
import tempfile
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STUDIO = Path(r"D:\Lzx\deveco studio\dev eco\DevEco Studio")

def call(args, cwd=None, env=None):
    result = subprocess.run([str(x) for x in args], cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    message = result.stdout.decode("utf-8", errors="replace")
    print(message[-9000:], flush=True)
    if result.returncode:
        raise RuntimeError(f"Build command failed with exit {result.returncode}")
    return message

def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--studio", type=Path, default=DEFAULT_STUDIO)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--target", default="3QC0124C03000514")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--source", type=Path, default=ROOT / "apps/workload")
    parser.add_argument("--bundle-prefix", default="io.memscene.workload.a")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "bin/apps")
    parser.add_argument("--signing-dir", type=Path, default=ROOT / "build/signing")
    parser.add_argument("--device-udid", help="reuse a previously verified UDID during offline artifact builds")
    args = parser.parse_args()
    studio = args.studio
    source = args.source.resolve()
    # Hvigor rejects non-ASCII project paths. Source stays in the user's project;
    # only the reproducible build cache is placed in an ASCII temporary directory.
    suffix = hashlib.sha256(str(ROOT).encode()).hexdigest()[:10]
    project = Path(tempfile.gettempdir()) / ("memscene_build_" + suffix)
    shutil.copytree(source, project, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("node_modules", "oh_modules", "build", ".hvigor"))
    lib = studio / "sdk/default/openharmony/toolchains/lib"
    java = studio / "jbr/bin/java.exe"
    keytool = studio / "jbr/bin/keytool.exe"
    output = args.output_dir.resolve()
    signing = args.signing_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    signing.mkdir(parents=True, exist_ok=True)
    icon = studio / "plugins/openharmony/lib/templates/ability/Native C++/code template/resources/media/icon.png"
    for directory in (project / "AppScope/resources/base/media",
                      project / "entry/src/main/resources/base/media"):
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(icon, directory / "icon.png")
    # Use the bundled plugin/runtime without downloading a second build toolchain.
    scoped = project / "node_modules/@ohos"
    scoped.mkdir(parents=True, exist_ok=True)
    for name in ("hvigor", "hvigor-ohos-plugin"):
        destination = scoped / name
        if not destination.exists():
            call(["cmd", "/c", "mklink", "/J", str(destination),
                  str(studio / "tools/hvigor" / name)])
    (project / "local.properties").write_text(
        "sdk.dir=" + (studio / "sdk/default").as_posix() + "\n", encoding="utf-8")
    env = os.environ.copy()
    env["NODE_HOME"] = str(studio / "tools/node")
    env["JAVA_HOME"] = str(studio / "jbr")
    env["DEVECO_SDK_HOME"] = str(studio / "sdk")
    env["PATH"] = str(studio / "tools/node") + os.pathsep + str(studio / "jbr/bin") + os.pathsep + env["PATH"]
    jar = lib / "hap-sign-tool.jar"
    p12 = lib / "OpenHarmony.p12"
    cert = signing / "app-release.pem"
    # The private-key entry's own certificate is self-signed. The SDK profile
    # supplies the CA-issued certificate for that same public key.
    template = json.loads((lib / "UnsgnedDebugProfileTemplate.json").read_text(encoding="utf-8"))
    cert.write_text(template["bundle-info"]["development-certificate"], encoding="utf-8")
    chain = signing / "app-release-chain.pem"
    chain_parts = [cert.read_text(encoding="utf-8")]
    for alias, name in (("openharmony application ca", "app-ca.pem"),
                        ("openharmony application root ca", "root-ca.pem")):
        part = signing / name
        if not part.exists():
            call([keytool, "-exportcert", "-rfc", "-alias", alias, "-keystore", p12,
                  "-storepass", "123456", "-file", part])
        chain_parts.append(part.read_text(encoding="utf-8"))
    chain.write_text("\n".join(chain_parts), encoding="utf-8")
    hdc = studio / "sdk/default/openharmony/toolchains/hdc.exe"
    if args.device_udid:
        udid = args.device_udid
    else:
        udid_output = call([hdc, "-t", args.target, "shell", "bm get -u"])
        udid = next(x.strip() for x in udid_output.splitlines() if len(x.strip()) == 64)
    app_file = project / "AppScope/app.json5"
    app_original = app_file.read_text(encoding="utf-8")
    manifest = []
    try:
        for index in range(1, args.count + 1):
            bundle = f"{args.bundle_prefix}{index}"
            config = json.loads(app_original)
            config["app"]["bundleName"] = bundle
            app_file.write_text(json.dumps(config), encoding="utf-8")
            call([studio / "tools/node/node.exe", studio / "tools/hvigor/bin/hvigorw.js",
                  "--mode", "module", "-p", "product=default", "-p", "module=entry@default",
                  "-p", "buildMode=debug", "assembleHap", "--no-daemon"], cwd=project, env=env)
            haps = list((project / "entry/build").rglob("*unsigned.hap"))
            if not haps:
                raise RuntimeError("No unsigned HAP produced")
            unsigned = max(haps, key=lambda p: p.stat().st_mtime)
            profile = json.loads((lib / "UnsgnedDebugProfileTemplate.json").read_text(encoding="utf-8"))
            profile["bundle-info"]["bundle-name"] = bundle
            profile["bundle-info"]["development-certificate"] = cert.read_text(encoding="utf-8")
            profile["debug-info"]["device-ids"] = [udid]
            profile["validity"] = {"not-before": int(time.time()) - 86400,
                                   "not-after": int(time.time()) + 365 * 86400}
            profile["acls"]["allowed-acls"] = []
            profile["permissions"]["restricted-permissions"] = []
            unsigned_profile = signing / f"a{index}.json"
            signed_profile = signing / f"a{index}.p7b"
            unsigned_profile.write_text(json.dumps(profile), encoding="utf-8")
            call([java, "-jar", jar, "sign-profile", "-mode", "localSign",
                  "-keyAlias", "openharmony application profile debug", "-keyPwd", "123456",
                  "-keystoreFile", p12, "-keystorePwd", "123456",
                  "-profileCertFile", lib / "OpenHarmonyProfileDebug.pem",
                  "-inFile", unsigned_profile, "-outFile", signed_profile,
                  "-signAlg", "SHA256withECDSA"])
            signed = output / f"a{index}.hap"
            call([java, "-jar", jar, "sign-app", "-mode", "localSign",
                  "-keyAlias", "openharmony application release", "-keyPwd", "123456",
                  "-keystoreFile", p12, "-keystorePwd", "123456", "-appCertFile", chain,
                  "-profileFile", signed_profile, "-inFile", unsigned, "-outFile", signed,
                  "-signAlg", "SHA256withECDSA"])
            manifest.append({"bundle": bundle, "path": str(signed.relative_to(ROOT)),
                             "sha256": hashlib.sha256(signed.read_bytes()).hexdigest(),
                             "signing": "OpenHarmony SDK debug", "device_udid": udid})
            if args.install:
                message = call([hdc, "-t", args.target, "install", signed])
                if "success" not in message.lower():
                    raise RuntimeError("Device did not accept the debug signature; use a vendor-approved signing profile")
    finally:
        app_file.write_text(app_original, encoding="utf-8")
        (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Built {len(manifest)} apps in {output}")

if __name__ == "__main__":
    main()
