"""Application services: the orchestration seam every interface shares, so there is one
implementation of "apply a model update" or "record an artifact". Services never touch argv, stdout
or the network, and never call an LLM.
"""
