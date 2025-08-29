#!/usr/bin/env python3
"""
Windows setup script for AWS Copier.
Helps install and configure AWS Copier on Windows systems.
"""

import subprocess
import sys
from pathlib import Path


def check_python_version():
    """Check if Python version is compatible."""
    if sys.version_info < (3, 8):
        print("❌ Python 3.8 or higher is required")
        print(f"Current version: {sys.version}")
        return False
    print(f"✅ Python version: {sys.version}")
    return True


def check_uv_installed():
    """Check if uv is installed."""
    try:
        result = subprocess.run(["uv", "--version"], capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ uv is installed: {result.stdout.strip()}")
            return True
        else:
            print("❌ uv is not working properly")
            return False
    except FileNotFoundError:
        print("❌ uv is not installed")
        print("Please install uv from: https://docs.astral.sh/uv/getting-started/installation/")
        return False


def install_uv():
    """Install uv on Windows."""
    print("🔧 Installing uv...")
    try:
        # Download and install uv using PowerShell
        subprocess.run(["powershell", "-Command", "irm https://astral.sh/uv/install.ps1 | iex"], check=True)
        print("✅ uv installed successfully")
        return True
    except subprocess.CalledProcessError:
        print("❌ Failed to install uv")
        return False


def setup_project():
    """Set up the project environment."""
    print("🔧 Setting up project environment...")
    try:
        # Sync dependencies
        subprocess.run(["uv", "sync"], check=True)
        print("✅ Dependencies installed")

        # Run tests to verify installation
        subprocess.run(["uv", "run", "pytest", "tests/unit/", "-v"], check=True)
        print("✅ Tests passed")

        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ Setup failed: {e}")
        return False


def create_example_config():
    """Create example configuration file."""
    config_path = Path("config.yaml")
    if config_path.exists():
        print(f"✅ Configuration file already exists: {config_path}")
        return True

    print("📝 Creating example configuration...")

    # Use Documents folder as default on Windows
    documents_path = Path.home() / "Documents"

    config_content = f"""# AWS Copier Configuration
aws_access_key_id: "your-access-key-id"
aws_secret_access_key: "your-secret-access-key"
aws_region: "us-east-1"
s3_bucket: "your-bucket-name"
s3_prefix: "backup"

# Folders to watch (Windows paths)
watch_folders:
  - "{documents_path}"
  # - "C:\\Users\\YourName\\Pictures"
  # - "C:\\Users\\YourName\\Desktop"

# Maximum concurrent uploads
max_concurrent_uploads: 100
"""

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(config_content)
        print(f"✅ Example configuration created: {config_path}")
        print("📝 Please edit the configuration with your AWS credentials")
        return True
    except Exception as e:
        print(f"❌ Failed to create configuration: {e}")
        return False


def create_windows_shortcuts():
    """Create Windows shortcuts and batch files."""
    print("🔧 Creating Windows shortcuts...")

    # Create batch file to run AWS Copier
    run_script = """@echo off
echo Starting AWS Copier...
cd /d "%~dp0"
uv run python main.py
pause
"""

    # Create batch file to kill AWS Copier
    kill_script = """@echo off
echo Stopping AWS Copier...
for /f "tokens=2" %%i in ('tasklist /fi "imagename eq python.exe" /fo table /nh ^| findstr main.py') do (
    echo Found AWS Copier process: %%i
    taskkill /pid %%i /f
)
echo AWS Copier stopped
pause
"""

    try:
        with open("run_aws_copier.bat", "w") as f:
            f.write(run_script)
        print("✅ Created run_aws_copier.bat")

        with open("stop_aws_copier.bat", "w") as f:
            f.write(kill_script)
        print("✅ Created stop_aws_copier.bat")

        return True
    except Exception as e:
        print(f"❌ Failed to create batch files: {e}")
        return False


def main():
    """Main setup function."""
    print("🚀 AWS Copier Windows Setup")
    print("=" * 40)

    # Check Python version
    if not check_python_version():
        return 1

    # Check if uv is installed
    if not check_uv_installed():
        if input("Install uv automatically? (y/N): ").lower().startswith("y"):
            if not install_uv():
                return 1
        else:
            print("Please install uv manually and run this script again")
            return 1

    # Setup project
    if not setup_project():
        return 1

    # Create configuration
    if not create_example_config():
        return 1

    # Create Windows shortcuts
    if not create_windows_shortcuts():
        return 1

    print("\n🎉 Setup completed successfully!")
    print("\nNext steps:")
    print("1. Edit config.yaml with your AWS credentials")
    print("2. Double-click run_aws_copier.bat to start")
    print("3. Use stop_aws_copier.bat to stop the service")

    return 0


if __name__ == "__main__":
    sys.exit(main())
