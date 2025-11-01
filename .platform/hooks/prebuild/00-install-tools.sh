#!/bin/bash
# Install development tools and compilers
echo "Installing build tools and C++ compilers..."
yum groupinstall "Development Tools" -y
yum install gcc gcc-c++ make -y

# Optional: update pip, wheel, and setuptools
python3 -m pip install --upgrade pip setuptools wheel
