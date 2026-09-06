"""Per-worker loopback ports; never accept arbitrary remote bridge addresses."""
import os

def loopback_port(name,default):
    value=os.environ.get(name,str(default))
    if not value.isascii() or not value.isdigit() or not 1024<=int(value)<=65535:
        raise ValueError('invalid loopback port: '+name)
    return int(value)

def loopback_url(name,default):
    return 'http://127.0.0.1:'+str(loopback_port(name,default))
