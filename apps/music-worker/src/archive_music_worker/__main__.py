import json
import sys
from .server import main
with open(sys.argv[1]) as stream: config = json.load(stream)
main(config)
