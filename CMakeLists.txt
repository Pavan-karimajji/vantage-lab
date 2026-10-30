cmake_minimum_required(VERSION 3.22)
project(adas-vantage-lab VERSION 0.1.0 LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# The lab is a standard component: generated lab components are shared
# libraries built under src/platform/lab_sil (VANTAGE_Lab_Design.md 4.3).
add_subdirectory(src/platform/lab_sil)
