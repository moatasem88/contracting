from setuptools import setup, find_packages

with open("requirements.txt") as f:
	install_requires = f.read().strip().split("\n")

# get version from __version__ variable in contracting/__init__.py
from contracting import __version__ as version

setup(
	name="contracting",
	version=version,
	description="contracting",
	author="kazem",
	author_email="kazemaraby@gmail.com",
	packages=find_packages(),
	zip_safe=False,
	include_package_data=True,
	install_requires=install_requires
)
