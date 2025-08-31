"""Simple YAML configuration for AWS Copier."""

from pathlib import Path
from typing import Any, Dict, List

import yaml


class SimpleConfig:
    """Simple configuration class using YAML."""

    def __init__(self, **kwargs):
        """Initialize configuration with default values."""
        # AWS Configuration
        self.aws_access_key_id: str = kwargs.get("aws_access_key_id", "YOUR_ACCESS_KEY_ID")
        self.aws_secret_access_key: str = kwargs.get("aws_secret_access_key", "YOUR_SECRET_ACCESS_KEY")
        self.aws_region: str = kwargs.get("aws_region", "us-east-1")
        self.s3_bucket: str = kwargs.get("s3_bucket", "your-bucket-name")
        self.s3_prefix: str = kwargs.get("s3_prefix", "")

        # Folders to watch - support both list format (legacy) and dict format (new)
        watch_folders_data = kwargs.get("watch_folders", [str(Path.home() / "Documents")])

        if isinstance(watch_folders_data, list):
            # Legacy format: list of folder paths
            self.watch_folders: List[Path] = [Path(p) for p in watch_folders_data]
            # Create default mapping using folder names
            self.folder_s3_mapping: Dict[Path, str] = {Path(p): Path(p).name for p in watch_folders_data}
        elif isinstance(watch_folders_data, dict):
            # New format: dict mapping folder paths to S3 names
            self.watch_folders: List[Path] = [Path(p) for p in watch_folders_data.keys()]
            self.folder_s3_mapping: Dict[Path, str] = {
                Path(folder_path): s3_name for folder_path, s3_name in watch_folders_data.items()
            }
        else:
            # Fallback to default
            default_path = Path.home() / "Documents"
            self.watch_folders: List[Path] = [default_path]
            self.folder_s3_mapping: Dict[Path, str] = {default_path: "Documents"}

        # File discovery output
        discovered_files_folder_data = kwargs.get(
            "discovered_files_folder", str(Path.home() / ".aws-copier" / "discovered")
        )
        self.discovered_files_folder: Path = Path(discovered_files_folder_data)

        # Upload settings
        self.max_concurrent_uploads: int = kwargs.get("max_concurrent_uploads", 100)

    @classmethod
    def load_from_yaml(cls, config_path: Path) -> "SimpleConfig":
        """Load configuration from YAML file."""
        if not config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {config_path}")

        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

        return cls(**data)

    def save_to_yaml(self, config_path: Path) -> None:
        """Save configuration to YAML file."""
        data = {
            "aws_access_key_id": self.aws_access_key_id,
            "aws_secret_access_key": self.aws_secret_access_key,
            "aws_region": self.aws_region,
            "s3_bucket": self.s3_bucket,
            "s3_prefix": self.s3_prefix,
            "watch_folders": {str(folder_path): s3_name for folder_path, s3_name in self.folder_s3_mapping.items()},
            "discovered_files_folder": str(self.discovered_files_folder),
            "max_concurrent_uploads": self.max_concurrent_uploads,
        }

        config_path.parent.mkdir(parents=True, exist_ok=True)

        with open(config_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, indent=2)

    def create_directories(self) -> None:
        """Create necessary directories."""
        self.discovered_files_folder.mkdir(parents=True, exist_ok=True)

    def get_s3_name_for_folder(self, folder_path: Path) -> str:
        """Get the S3 name for a given folder path.

        Args:
            folder_path: Local folder path

        Returns:
            S3 name for the folder, or folder name as fallback
        """
        return self.folder_s3_mapping.get(folder_path, folder_path.name)

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to dictionary."""
        return {
            "aws_access_key_id": self.aws_access_key_id,
            "aws_secret_access_key": self.aws_secret_access_key,
            "aws_region": self.aws_region,
            "s3_bucket": self.s3_bucket,
            "s3_prefix": self.s3_prefix,
            "watch_folders": {str(folder_path): s3_name for folder_path, s3_name in self.folder_s3_mapping.items()},
            "discovered_files_folder": str(self.discovered_files_folder),
            "max_concurrent_uploads": self.max_concurrent_uploads,
        }


# Default configuration path
DEFAULT_CONFIG_PATH = Path.home() / "aws-copier-config.yaml"


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> SimpleConfig:
    """Load configuration from YAML file or create default."""
    if config_path.exists():
        return SimpleConfig.load_from_yaml(config_path)
    # Create a template configuration
    config = SimpleConfig()
    config.save_to_yaml(config_path)
    print(f"Template configuration created at: {config_path}")
    print("Please edit the YAML configuration file with your AWS credentials and settings.")
    return config
