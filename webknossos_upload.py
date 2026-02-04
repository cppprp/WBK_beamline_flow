import webknossos as wk
import cluster_tools
from tempfile import TemporaryDirectory
import argparse
from webknossos import webknossos_context, Team
from pathlib import Path
import sys
def get_auth_token():
    """Read authentication token from config file."""
    script_dir = Path(__file__).parent
    token_file = script_dir / '.config' / 'auth_token.txt'
    return token_file.read_text().strip()

