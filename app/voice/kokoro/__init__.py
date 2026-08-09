"""JARVIS's neural voice: Kokoro 82M, running locally on ONNX Runtime.

Split across small modules on purpose (assets, normalise, g2p, engine)
because the licence-sensitive part is the *pronunciation* path, and it
has to be reviewable on its own.
"""
