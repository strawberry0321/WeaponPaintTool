"""Bound native BLAS workers before NumPy is loaded by the export pipeline.

The baker already runs independent jobs. Its narrow 3/6-column products do not
benefit from a second large native thread pool inside each job.
"""
import os
os.environ['OPENBLAS_NUM_THREADS']='1'
os.environ['GOTO_NUM_THREADS']='1'
import numpy as np

BLAS_THREADS = 1
