"""avd.py: the v1 avd-init cases, the registry rules and the system image."""

from dataclasses import replace

import pytest

from appliance import avd


def lines(path):
    return path.read_text().splitlines()


def test_create(cfg):
    marker = avd.create(cfg, 35)
    avd.configure(cfg, 35)
    config = lines(cfg.avd_dir / "config.ini")
    assert "image.sysdir.1=system-images/android-35/google_apis_playstore/x86_64/" in config
    for line in ("AvdId=android", "tag.id=google_apis_playstore", "PlayStore.enabled=true", "disk.dataPartition.size=4G",
                 "hw.ramSize=2048", "hw.cpu.ncore=2", "hw.lcd.width=1080", "hw.lcd.height=1920", "hw.lcd.density=420",
                 "fastboot.forceColdBoot=yes", "fastboot.forceFastBoot=no", "hw.keyboard=yes", "hw.gpu.enabled=yes"):  # fmt: skip
        assert line in config
    assert (cfg.avd_dir / "quickbootChoice.ini").read_text() == "saveOnExit=false\n"
    # The registry of the state directory, as v1 writes it.
    assert lines(cfg.avd_home / "android.ini") == [
        "avd.ini.encoding=UTF-8",
        f"path={cfg.avd_dir}",
        "path.rel=avd/android.avd",
        "target=android-35",
    ]
    # The registry that the emulator reads, on tmpfs.
    assert lines(cfg.registry_dir / "android.ini") == ["avd.ini.encoding=UTF-8", f"path={cfg.avd_dir}", "target=android-35"]
    assert marker and avd.first_boot_marker(cfg) == marker


def test_spaced_equal_values_stay_and_changes_replace(cfg):
    avd.create(cfg, 35)
    avd.configure(cfg, 35)
    config = cfg.avd_dir / "config.ini"
    # The emulator rewrites config.ini as "key = value". An equal value stays.
    text = config.read_text().replace("hw.ramSize=2048", "hw.ramSize = 2048M").replace("hw.cpu.ncore=2", "hw.cpu.ncore = 2")
    config.write_text(text)
    (cfg.avd_dir / "userdata-qemu.img").touch()
    before = config.stat().st_mtime_ns
    avd.configure(cfg, 35)
    assert "hw.ramSize = 2048M" in lines(config)
    assert config.stat().st_mtime_ns == before
    assert (cfg.avd_dir / "userdata-qemu.img").exists()
    # A changed option replaces the spaced form and adds no duplicate.
    avd.configure(replace(cfg, memory_mib=3072, cores=4), 35)
    config_lines = lines(config)
    assert "hw.ramSize=3072" in config_lines and "hw.cpu.ncore=4" in config_lines
    assert sum(line.startswith("hw.ramSize") for line in config_lines) == 1
    assert sum(line.startswith("hw.cpu.ncore") for line in config_lines) == 1


def test_wrong_api_level_is_refused_untouched(cfg):
    avd.create(cfg, 36)
    avd.configure(cfg, 36)
    files = {p: p.read_bytes() for p in cfg.avd_dir.iterdir()}
    registry = (cfg.registry_dir / "android.ini").read_text()
    with pytest.raises(avd.AvdError, match="does not upgrade existing AVDs") as err:
        avd.configure(cfg, 35)
    assert "system-images/android-36/google_apis_playstore/x86_64" in str(err.value)
    assert {p: p.read_bytes() for p in cfg.avd_dir.iterdir()} == files
    assert (cfg.registry_dir / "android.ini").read_text() == registry
    # The matching level is adopted unchanged.
    avd.configure(cfg, 36)


def test_v1_registry_is_never_edited(cfg):
    avd.create(cfg, 35)
    # A v1 registry holds the host path; a rollback to v1 needs it.
    v1 = "avd.ini.encoding=UTF-8\npath=/var/lib/juno/android/avd/android.avd\npath.rel=avd/android.avd\ntarget=android-35\n"
    (cfg.avd_home / "android.ini").write_text(v1)
    avd.configure(cfg, 35)
    assert (cfg.avd_home / "android.ini").read_text() == v1
    assert f"path={cfg.avd_dir}" in lines(cfg.registry_dir / "android.ini")
    # A missing v1 registry is not created for an existing AVD either.
    (cfg.avd_home / "android.ini").unlink()
    avd.configure(cfg, 35)
    assert not (cfg.avd_home / "android.ini").exists()


def test_low_resolution_moves_to_native(cfg):
    avd.create(cfg, 35)
    config = cfg.avd_dir / "config.ini"
    text = config.read_text().replace("hw.lcd.width=1080", "hw.lcd.width=320")
    config.write_text(text.replace("hw.lcd.height=1920", "hw.lcd.height=640").replace("hw.lcd.density=420", "hw.lcd.density=160"))
    (cfg.avd_dir / "userdata-qemu.img").touch()
    avd.configure(cfg, 35)
    assert {"hw.lcd.width=1080", "hw.lcd.height=1920", "hw.lcd.density=420"} <= set(lines(config))
    assert (cfg.avd_dir / "userdata-qemu.img").exists()


def test_snapshot_keys_are_forced(cfg):
    avd.create(cfg, 35)
    config = cfg.avd_dir / "config.ini"
    config.write_text(config.read_text().replace("fastboot.forceColdBoot=yes", "fastboot.forceColdBoot = no"))
    (cfg.avd_dir / "quickbootChoice.ini").write_text("saveOnExit=true\n")
    avd.configure(cfg, 35)
    assert "fastboot.forceColdBoot=yes" in lines(config)
    assert (cfg.avd_dir / "quickbootChoice.ini").read_text() == "saveOnExit=false\n"


def test_delete_needs_the_marker(cfg):
    marker = avd.create(cfg, 35)
    with pytest.raises(avd.AvdError):
        avd.delete(cfg, "other")
    avd.first_boot_done(cfg)
    with pytest.raises(avd.AvdError):
        avd.delete(cfg, marker)
    assert avd.exists(cfg)


def test_create_into_existing_files_gets_no_marker(cfg):
    cfg.avd_dir.mkdir(parents=True)
    (cfg.avd_dir / "userdata-qemu.img").write_text("old userdata")
    assert avd.create(cfg, 35) is None
    assert (cfg.avd_dir / "userdata-qemu.img").read_text() == "old userdata"


def test_system_image(cfg, fake):
    assert avd.system_image(cfg) == 35
    assert avd.system_image(replace(cfg, api_level=35)) == 35
    with pytest.raises(avd.AvdError, match=r"APPLIANCE_API_LEVEL is 36.*35"):
        avd.system_image(replace(cfg, api_level=36))
    (fake.sdk / "system-images" / "android-36" / "google_apis_playstore" / "x86_64").mkdir(parents=True)
    with pytest.raises(avd.AvdError, match="set APPLIANCE_API_LEVEL"):
        avd.system_image(cfg)
    assert avd.system_image(replace(cfg, api_level=36)) == 36
    # Other tags do not count.
    with pytest.raises(avd.AvdError, match="no google_apis_playstore"):
        avd.system_image(replace(cfg, sdk_root=cfg.sdk_root / "missing"))
