{
  "targets": [
    {
      "target_name": "webrtc_addon",
      "sources": ["src/addon.cc", "src/peer_connection_wrapper.cc"],
      "include_dirs": [
        "<!@(node -p \"require('node-addon-api').include\")",
        "../../webrtc/src",
        "../../webrtc/src/third_party/abseil-cpp",
        "../../webrtc/src/buildtools/third_party/libc++",
        "../../webrtc/src/third_party/libc++/src/include",
        "../../webrtc/src/third_party/libc++abi/src/include"
      ],
      "defines": [
        "NAPI_VERSION=8",
        "NAPI_DISABLE_CPP_EXCEPTIONS",
        "WEBRTC_POSIX",
        "WEBRTC_LINUX",
        "WEBRTC_USE_H264"
      ],
      "conditions": [
        [
          "OS=='linux'",
          {
            "libraries": [
              "<(module_root_dir)/../../webrtc/src/out/release_x64/obj/libwebrtc.a",
              "-lX11",
              "-lpthread",
              "-ldl",
              "-lrt"
            ],
            "cflags_cc": [
              "-std=c++20",
              "-fPIC",
              "-fno-exceptions"
            ]
          }
        ]
      ]
    }
  ]
}
