from pathlib import Path

import pytest

from appliance import config


def test_defaults():
    cfg = config.load({})
    assert cfg.avd_name == "android"
    assert cfg.api_level is None
    assert cfg.gpu == "auto"
    assert cfg.idle_stop_seconds == 3600
    assert (cfg.memory_mib, cfg.cores, cfg.disk_size, cfg.boot_timeout) == (4096, 4, "32G", 900)
    assert config.load({"APPLIANCE_MAX_UPLOAD_MIB": "10"}).max_upload_mib == 10 and cfg.max_upload_mib == 4096
    assert cfg.data_dir == Path("/data")
    assert cfg.socket_path == Path("/run/appliance/api.sock")
    assert cfg.emulator_bin == "/opt/android-sdk/emulator/emulator"
    assert cfg.adb_bin == "/opt/android-sdk/platform-tools/adb"
    assert cfg.registry_dir == Path("/tmp/appliance/avd")
    assert cfg.lock_file == Path("/data/appliance/daemon.lock")


def test_values_and_empty_keys():
    cfg = config.load(
        {
            "APPLIANCE_AVD_NAME": "pixel-1",
            "APPLIANCE_API_LEVEL": "35",
            "APPLIANCE_GPU": "lavapipe",
            "APPLIANCE_IDLE_STOP_MINUTES": "0",
            "APPLIANCE_MEMORY_MIB": "3072",
            "APPLIANCE_TRUST_PROXY": "1",
            # An empty value (APPLIANCE_TURN_HOST= in the file) means unset.
            "APPLIANCE_DISK_SIZE": "",
            "APPLIANCE_DATA_DIR": "/var/tmp/x",
        }
    )
    assert (cfg.avd_name, cfg.api_level, cfg.gpu, cfg.idle_stop_seconds) == ("pixel-1", 35, "lavapipe", 0)
    assert cfg.memory_mib == 3072 and cfg.trust_proxy and cfg.disk_size == "32G"
    assert cfg.avd_dir == Path("/var/tmp/x/avd/pixel-1.avd")


@pytest.mark.parametrize(
    "key, value",
    [
        ("APPLIANCE_GPU", "swiftshader_indirect"),
        ("APPLIANCE_CORES", "0"),
        ("APPLIANCE_CORES", "four"),
        ("APPLIANCE_IDLE_STOP_MINUTES", "-1"),
        ("APPLIANCE_AVD_NAME", "../etc"),
        ("APPLIANCE_DISK_SIZE", "32 G"),
        ("APPLIANCE_TRUST_PROXY", "yes"),
        ("APPLIANCE_MAX_UPLOAD_MIB", "0"),
        ("APPLIANCE_MAX_UPLOAD_MIB", "big"),
    ],
)
def test_invalid_values_are_refused(key, value):
    with pytest.raises(config.ConfigError, match=key):
        config.load({key: value})


def test_config_file_fills_unset_keys(tmp_path):
    conf = tmp_path / "appliance.conf"
    conf.write_text("# comment\n\nAPPLIANCE_CORES=2\nAPPLIANCE_MEMORY_MIB=3072\n bad line\n1BAD=x\nAPPLIANCE_CORES=8\nAPPLIANCE_GPU=host")
    cfg = config.load({"APPLIANCE_CONFIG": str(conf), "APPLIANCE_MEMORY_MIB": "2048"})
    # The environment wins; the first line of a key wins in the file.
    assert (cfg.cores, cfg.memory_mib, cfg.gpu) == (2, 2048, "host")


def test_android_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPLAY", ":7")
    monkeypatch.delenv("USER", raising=False)
    cfg = config.load({"APPLIANCE_DATA_DIR": str(tmp_path), "APPLIANCE_TMP_DIR": "/t"})
    env = cfg.android_env()
    assert env["HOME"] == f"{tmp_path}/home"
    assert env["ANDROID_USER_HOME"] == env["ANDROID_EMULATOR_HOME"] == f"{tmp_path}/home/.android"
    assert env["ANDROID_AVD_HOME"] == "/t/appliance/avd"
    assert env["ANDROID_SDK_ROOT"] == "/opt/android-sdk"
    assert env["ANDROID_ADB_SERVER_PORT"] == "5037"
    assert env["USER"] == "android"
    assert "DISPLAY" not in env
