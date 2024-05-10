from setuptools import setup, find_packages

setup(
    name='mypackage',
    version='0.1',
    packages=find_packages(),
    entry_points={
        'console_scripts': [
            'mycommand = mypackage.myscript:main'
        ]
    },
    install_requires=[
        # List your project dependencies here.
        # e.g., 'requests', 'boto3', etc.
    ],
)


# Function to read the requirements from the requirements.txt file
def load_requirements(filename='requirements.txt'):
    with open(filename, 'r') as file:
        return file.read().splitlines()

setup(
    name='mypackage',
    version='0.1',
    packages=find_packages(),
    entry_points={
        'console_scripts': [
            'mycommand = mypackage.myscript:main'
        ]
    },
    install_requires=load_requirements(),
)