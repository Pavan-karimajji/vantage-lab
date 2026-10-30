find_package(AdasInterfaces REQUIRED)

include(${CMAKE_CURRENT_LIST_DIR}/lab_components.cmake)

# One generated topology per catalogue file; the list is re-globbed when a
# file is added or removed.
file(GLOB LAB_TOPOLOGY_FILES CONFIGURE_DEPENDS
    "${CMAKE_CURRENT_SOURCE_DIR}/../../../catalogue/*.yaml")
foreach(LAB_TOPOLOGY_FILE IN LISTS LAB_TOPOLOGY_FILES)
    lab_add_topology("${LAB_TOPOLOGY_FILE}")
endforeach()
