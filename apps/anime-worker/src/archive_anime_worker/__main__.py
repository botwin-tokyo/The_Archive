import json,sys
from .server import main
main(json.load(open(sys.argv[1])))
