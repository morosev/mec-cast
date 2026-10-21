# mec-cast-e2-bridge, built inside FlexRIC's own CMake tree so it links the
# same e42_xapp library, with its ASN.1 dependencies, and the same E2AP/KPM
# selection as FlexRIC's examples. deploy/lab/ric/flexric/Dockerfile adds this
# directory to the FlexRIC checkout's build; FlexRIC itself is not changed.
add_executable(mec-cast-e2-bridge ${CMAKE_CURRENT_LIST_DIR}/mec_cast_e2_bridge.c)
target_include_directories(mec-cast-e2-bridge PRIVATE ${CMAKE_SOURCE_DIR})
target_compile_definitions(mec-cast-e2-bridge PRIVATE ${E2AP_VERSION} ${KPM_VERSION})
target_link_libraries(mec-cast-e2-bridge PUBLIC e42_xapp -pthread -lsctp -ldl)
