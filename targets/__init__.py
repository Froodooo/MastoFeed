"""Posting targets.

Each target module provides:
  NAME        short identifier, stored in the database
  enabled()   whether the target is configured
  post(guid, title, link) -> str   posts the entry and returns its URL
"""
