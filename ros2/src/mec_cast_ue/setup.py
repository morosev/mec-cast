from setuptools import setup

package_name = "mec_cast_ue"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    description="UE agent: N lidar + M render instances in one process.",
    license="MIT",
    entry_points={
        "console_scripts": [
            "ue_agent = mec_cast_ue.ue_agent:main",
        ],
    },
)
