# DTensor MLIR dialect.
load("@llvm-project//mlir:tblgen.bzl", "gentbl_cc_library", "td_library")

package(
    default_visibility = [
        "//tensorflow/dtensor:dtensor-internal",
        # Allow visibility from the mlir language server.
        "//learning/brain/mlir/mlir_lsp_server:__pkg__",
    ],
    licenses = ["notice"],
)

# ODS (https://mlir.llvm.org/docs/OpDefinitions/) generation for op and dialect files to include.

td_library(
    name = "dtensor_td_files",
    srcs = [
        "ir/dtensor_dialect.td",
        "ir/dtensor_ops.td",
    ],
    deps = [
        "@llvm-project//mlir:FuncTdFiles",
        "@llvm-project//mlir:InferTypeOpInterfaceTdFiles",
        "@llvm-project//mlir:OpBaseTdFiles",
    ],
)

gentbl_cc_library(
    name = "DialectIncGen",
    tbl_outs = [
        (
            ["-gen-op-decls"],
            "ir/ops.h.inc",
        ),
        (
            ["-gen-op-defs"],
            "ir/ops.cc.inc",
        ),
        (
            ["-gen-dialect-decls"],
            "ir/dialect.h.inc",
        ),
        (
            ["-gen-dialect-defs"],
            "ir/dialect.cc.inc",
        ),
    ],
    tblgen = "@llvm-project//mlir:mlir-tblgen",
    td_file = "ir/dtensor_ops.td",
    deps = [":dtensor_td_files"],
)

cc_library(
    name = "Dialect",
    srcs = ["ir/ops.cc"],
    hdrs = [
        "ir/dialect.h",
        "ir/ops.h",
    ],
    deps = [
        ":DialectIncGen",
        ":ir/dtensor_attributes",
        "//tensorflow/compiler/mlir:string_container_utils",
        "//tensorflow/dtensor/cc:tensor_layout",
        "@com_google_absl//absl/strings",
        "@llvm-project//mlir:IR",
        "@llvm-project//mlir:InferTypeOpInterface",
    ],
)

cc_library(
    name = "ir/dtensor_attributes",
    srcs = ["ir/dtensor_attributes.cc"],
    hdrs = [
        "ir/dialect.h",
        "ir/dtensor_attributes.h",
    ],
    deps = [
        ":DialectIncGen",
        "//tensorflow/dtensor/cc:tensor_layout",
        "//tensorflow/dtensor/proto:layout_proto_cc",
        "@llvm-project//llvm:Support",
        "@llvm-project//mlir:IR",
    ],
)
