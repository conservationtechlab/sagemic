"""Tools needed by both Birbler and SageMic.
"""
import os


def check_path(date, base_path):
    """Create new folder for date to store detections.

    Args:
        date (str): Current date in YYYY-MM-DD.

    Returns:
        str: The path for where the detections will be stored that day.
    """
    new_path = os.path.join(base_path, date)
    print(new_path)
    if not os.path.exists(new_path):
        os.makedirs(new_path)
        print(f"Directory created: {os.path.abspath(new_path)}")

    return new_path
