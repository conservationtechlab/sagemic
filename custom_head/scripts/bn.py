# bn.py: BirdNET analyzer that can also return embeddings
from birdnetlib.analyzer import Analyzer
try:
    import tflite_runtime.interpreter as tflite
except ImportError:
    from tensorflow import lite as tflite

def make_analyzer(**kwargs):
    a = Analyzer(**kwargs)
    # Keep internal layers so the embedding layer can be read
    a.interpreter = tflite.Interpreter(
        model_path=a.model_path, num_threads=1,
        experimental_preserve_all_tensors=True,
    )
    a.interpreter.allocate_tensors()
    return a
