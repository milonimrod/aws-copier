"""Regression tests for the Docker/NAS deployment artifacts (Dockerfile, compose, example config)."""

from pathlib import Path

import yaml

from aws_copier.models.simple_config import SimpleConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_docker_compose_yaml_is_valid():
    """docker-compose.yml parses as valid YAML with the expected service and volumes."""
    compose_path = REPO_ROOT / "docker-compose.yml"
    data = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    assert "aws-copier" in data["services"]
    service = data["services"]["aws-copier"]
    assert service["restart"] == "unless-stopped"

    volumes = service["volumes"]
    assert any(v.startswith("./config.yaml:/app/config.yaml") for v in volumes)


def test_dockerfile_references_entrypoint_script():
    """Dockerfile installs and declares docker/entrypoint.sh as its ENTRYPOINT."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY docker/entrypoint.sh /entrypoint.sh" in dockerfile
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in dockerfile


def test_entrypoint_script_exists_and_is_executable():
    """docker/entrypoint.sh exists on disk with the executable bit set."""
    entrypoint = REPO_ROOT / "docker" / "entrypoint.sh"

    assert entrypoint.exists()
    assert entrypoint.stat().st_mode & 0o111, "entrypoint.sh must be executable"


def test_config_yaml_example_loads_into_simple_config():
    """config.yaml.example is valid YAML that SimpleConfig can load without error."""
    example_path = REPO_ROOT / "config.yaml.example"
    config = SimpleConfig.load_from_yaml(example_path)

    assert config.s3_bucket == "your-bucket-name"
    assert config.web_port == 8765
    assert config.web_enabled is True
    # watch_folders keys are container paths matching docker-compose.yml's mount targets.
    assert Path("/data/documents") in config.watch_folders
    assert config.get_s3_name_for_folder(Path("/data/documents")) == "Documents"


def test_config_yaml_example_matches_compose_mount_targets():
    """Every watch_folders key in config.yaml.example has a matching mount target in compose."""
    config = SimpleConfig.load_from_yaml(REPO_ROOT / "config.yaml.example")
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    volumes = compose["services"]["aws-copier"]["volumes"]
    mount_targets = {v.split(":")[1] for v in volumes if not v.startswith("./config.yaml")}

    for folder in config.watch_folders:
        assert str(folder) in mount_targets, f"{folder} has no matching docker-compose mount"
