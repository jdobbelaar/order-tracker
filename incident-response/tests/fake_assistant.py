"""Stands in for the coding assistant: echoes the start of the prompt it received on stdin."""
import sys

prompt = sys.stdin.read()
print("FAKE REPORT")
print(prompt.splitlines()[0])
