from setuptools import find_packages, setup

package_name = "mec_cast_admin_client"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    tests_require=["pytest"],
    extras_require={"test": ["pytest"]},
    zip_safe=True,
    maintainer="morosev",
    maintainer_email="morosev@gmail.com",
    description="Admin control-plane client shared by the mec-cast ROS2 nodes.",
    license="MIT",
    entry_points={"console_scripts": []},
)
