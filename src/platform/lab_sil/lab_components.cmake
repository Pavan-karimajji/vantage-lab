# lab_add_topology(<file>): generate one topology's lab components at configure time and build
# each as a shared library on the generic SIL ABI, in its own folder with its config bundle
# beside it (VANTAGE_Lab_Design.md 4.3, R-SIM-3a). Also writes <lab dir>/<topology>/index.json.
include_guard(GLOBAL)

find_package(Python3 COMPONENTS Interpreter REQUIRED)
# The standard functions of `interfaces` (found through CMAKE_MODULE_PATH by find_package(AdasInterfaces)).
include(sil_ports_generate)
include(config_bundle_pack)

function(lab_add_topology TOPOLOGY_FILE)
    get_filename_component(_topology "${TOPOLOGY_FILE}" NAME_WE)
    # catalogue/provisional.yaml holds values, not a topology; the glob of the caller matches it too.
    if(_topology STREQUAL "provisional")
        return()
    endif()
    get_filename_component(_catalogue_dir "${TOPOLOGY_FILE}" DIRECTORY)
    get_filename_component(_lab_root "${CMAKE_CURRENT_FUNCTION_LIST_DIR}/../../.." ABSOLUTE)
    set(_gen_dir "${CMAKE_BINARY_DIR}/lab_generated")
    set(_lab_dir "${CMAKE_BINARY_DIR}/lab")

    # Editing the topology, the calibration values, the template or the generator re-configures.
    file(GLOB _generator_files CONFIGURE_DEPENDS "${_lab_root}/src/lab_tools/*.py" "${_lab_root}/template/*.in")
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
        "${TOPOLOGY_FILE}" "${_catalogue_dir}/provisional.yaml" ${_generator_files})

    execute_process(
        COMMAND "${CMAKE_COMMAND}" -E env "PYTHONPATH=${_lab_root}/src"
                "${Python3_EXECUTABLE}" -m lab_tools.generate
                --topology "${TOPOLOGY_FILE}" --out "${_gen_dir}" --lab-dir "${_lab_dir}"
                --library-prefix "${CMAKE_SHARED_LIBRARY_PREFIX}" --library-suffix "${CMAKE_SHARED_LIBRARY_SUFFIX}"
        WORKING_DIRECTORY "${_lab_root}"
        RESULT_VARIABLE _rc
        OUTPUT_VARIABLE _out
        ERROR_VARIABLE _err)
    if(NOT _rc EQUAL 0)
        message(FATAL_ERROR "lab_add_topology(${_topology}): the generator failed (${_rc})\n${_out}${_err}")
    endif()

    file(READ "${_lab_dir}/${_topology}/index.json" _index)
    string(JSON _components GET "${_index}" components)
    string(JSON _count LENGTH "${_components}")
    math(EXPR _last "${_count} - 1")
    if(_count EQUAL 0)
        return()
    endif()

    set(_project_name "${ADAS_PROJECT}")
    if(NOT _project_name)
        set(_project_name "base")
    endif()

    foreach(_i RANGE ${_last})
        string(JSON _comp MEMBER "${_components}" ${_i})
        string(JSON _build_id GET "${_components}" ${_comp} build_id)
        string(TOUPPER "${_comp}" _C)
        set(_lib "lab_${_topology}_${_comp}")
        set(_comp_gen "${_gen_dir}/${_topology}/${_comp}")
        set(_comp_out "${_lab_dir}/${_topology}/${_comp}")

        add_library(${_lib} SHARED "${_comp_gen}/lab_sil.cpp" "${_comp_gen}/lab_component.hpp")
        target_include_directories(${_lib} PRIVATE "${_comp_gen}")
        target_link_libraries(${_lib} PRIVATE AdasInterfaces::AdasInterfaces ${CMAKE_DL_LIBS})
        target_compile_definitions(${_lib} PRIVATE
            ADAS_SIL_ABI_EXPORTS
            "ADAS_${_C}_VERSION=\"${PROJECT_VERSION}\""
            "ADAS_${_C}_BUILD_ID=\"${_build_id}\""
            "ADAS_${_C}_PROJECT_NAME=\"${_project_name}\"")
        set_target_properties(${_lib} PROPERTIES POSITION_INDEPENDENT_CODE ON)

        # One folder per library, so <library_dir>/config/ is this component's bundle only.
        set_target_properties(${_lib} PROPERTIES
            RUNTIME_OUTPUT_DIRECTORY "${_comp_out}"
            LIBRARY_OUTPUT_DIRECTORY "${_comp_out}")
        foreach(_cfg DEBUG RELEASE RELWITHDEBINFO MINSIZEREL)
            set_target_properties(${_lib} PROPERTIES
                RUNTIME_OUTPUT_DIRECTORY_${_cfg} "${_comp_out}"
                LIBRARY_OUTPUT_DIRECTORY_${_cfg} "${_comp_out}")
        endforeach()

        adas_generate_sil_ports(${_lib}
            PORTS_YAML "${_comp_gen}/ports.yaml"
            COMPONENT_ROOT "${_comp_gen}"
            OUT_DIR "${CMAKE_CURRENT_BINARY_DIR}/lab_ports/${_topology}/${_comp}")
        adas_pack_config_bundle(${_lib}
            DEFAULT_YAML "${_comp_gen}/default.yaml"
            EGO_PARAMS_YAML "${_comp_gen}/ego_params.yaml"
            PORTS_YAML "${_comp_gen}/ports.yaml")
    endforeach()
endfunction()
