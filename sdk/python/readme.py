# imports
import contextlib
import os
import json
import glob
import argparse

# define constants
ENABLE_MANUAL_CALLING = True  # defines whether the workflow can be invoked or not
NOT_TESTED_NOTEBOOKS = [
    "datastore",
    "mlflow-model-local-inference-test",
    "multicloud-configuration",
    "debug-online-endpoints-locally-in-visual-studio-code",
    "train-hyperparameter-tune-with-sklearn",
    "train-hyperparameter-tune-deploy-with-keras",
    "train-hyperparameter-tune-deploy-with-tensorflow",
    "online-endpoints-managed-identity-sai",
    "online-endpoints-managed-identity-uai",
]  # cannot automate lets exclude
NOT_SCHEDULED_NOTEBOOKS = []  # these are too expensive, lets not run everyday
# define branch where we need this
# use if running on a release candidate, else make it empty
BRANCH = "main"  # default - do not change
# BRANCH = "sdk-preview"  # this should be deleted when this branch is merged to main
GITHUB_CONCURRENCY_GROUP = (
    "${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}"
)


def main(args):

    # get list of notebooks
    notebooks = sorted(glob.glob("**/*.ipynb", recursive=True))

    # write workflows
    write_workflows(notebooks)

    # modify notebooks
    modify_notebooks(notebooks)

    # write readme
    write_readme(notebooks)

    # write pipeline readme
    pipeline_dir = "jobs" + os.sep + "pipelines" + os.sep
    with change_working_dir(pipeline_dir):
        pipeline_notebooks = sorted(glob.glob("**/*.ipynb", recursive=True))
    pipeline_notebooks = [
        f"{pipeline_dir}{notebook}" for notebook in pipeline_notebooks
    ]
    write_readme(pipeline_notebooks, pipeline_folder=pipeline_dir)


def write_workflows(notebooks):
    print("writing .github/workflows...")
    for notebook in notebooks:
        if not any(excluded in notebook for excluded in NOT_TESTED_NOTEBOOKS):
            # get notebook name
            name = os.path.basename(notebook).replace(".ipynb", "")
            folder = os.path.dirname(notebook)
            classification = folder.replace(os.sep, "-")

            enable_scheduled_runs = True
            if any(excluded in notebook for excluded in NOT_SCHEDULED_NOTEBOOKS):
                enable_scheduled_runs = False

            # write workflow file
            write_notebook_workflow(
                notebook, name, classification, folder, enable_scheduled_runs
            )
    print("finished writing .github/workflows")


def get_mlflow_import(notebook):
    with open(notebook, "r", encoding="utf-8") as f:
        if "import mlflow" in f.read():
            return """
    - name: pip install mlflow reqs
      run: pip install -r sdk/python/mlflow-requirements.txt"""
        else:
            return ""


def write_notebook_workflow(
    notebook, name, classification, folder, enable_scheduled_runs
):
    is_pipeline_notebook = ("jobs-pipelines" in classification) or (
        "assets-component" in classification
    )
    creds = "${{secrets.AZUREML_CREDENTIALS}}"
    # Duplicate name in working directory during checkout
    # https://github.com/actions/checkout/issues/739
    github_workspace = "${{ github.workspace }}"
    mlflow_import = get_mlflow_import(notebook)
    posix_folder = folder.replace(os.sep, "/")
    posix_notebook = notebook.replace(os.sep, "/")

    workflow_yaml = f"""name: sdk-{classification}-{name}
# This file is created by sdk/python/readme.py.
# Please do not edit directly.
on:\n"""
    if ENABLE_MANUAL_CALLING:
        workflow_yaml += f"""  workflow_dispatch:\n"""
    if enable_scheduled_runs:
        workflow_yaml += f"""  schedule:
    - cron: "0 */8 * * *"\n"""
    workflow_yaml += f"""  pull_request:
    branches:
      - main\n"""
    if BRANCH != "main":
        workflow_yaml += f"""      - {BRANCH}\n"""
        if is_pipeline_notebook:
            workflow_yaml += "      - pipeline/*\n"
    workflow_yaml += f"""    paths:
      - sdk/python/{posix_folder}/**
      - .github/workflows/sdk-{classification}-{name}.yml
      - sdk/python/dev-requirements.txt
      - infra/**
      - sdk/python/setup.sh
concurrency:
  group: {GITHUB_CONCURRENCY_GROUP}
  cancel-in-progress: true
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
    - name: check out repo
      uses: actions/checkout@v2
    - name: setup python
      uses: actions/setup-python@v2
      with: 
        python-version: "3.8"
    - name: pip install notebook reqs
      run: pip install -r sdk/python/dev-requirements.txt{mlflow_import}
    - name: azure login
      uses: azure/login@v1
      with:
        creds: {creds}
    - name: bootstrap resources
      run: |
          echo '{GITHUB_CONCURRENCY_GROUP}';
          bash bootstrap.sh
      working-directory: infra
      continue-on-error: false
    - name: setup SDK
      run: |
          source "{github_workspace}/infra/sdk_helpers.sh";
          source "{github_workspace}/infra/init_environment.sh";
          bash setup.sh
      working-directory: sdk/python
      continue-on-error: true
    - name: setup-cli
      run: |
          source "{github_workspace}/infra/sdk_helpers.sh";
          source "{github_workspace}/infra/init_environment.sh";
          bash setup.sh
      working-directory: cli
      continue-on-error: true
    - name: run {posix_notebook}
      run: |
          source "{github_workspace}/infra/sdk_helpers.sh";
          source "{github_workspace}/infra/init_environment.sh";
          bash "{github_workspace}/infra/sdk_helpers.sh" generate_workspace_config "../../.azureml/config.json";
          bash "{github_workspace}/infra/sdk_helpers.sh" replace_template_values "{name}.ipynb";
          [ -f "../../.azureml/config" ] && cat "../../.azureml/config";"""

    if name == "debug-online-endpoints-locally-in-visual-studio-code":
        workflow_yaml += f"""
          sed -i -e "s/<ENDPOINT_NAME>/localendpoint/g" {name}.ipynb

          # Create a dummy executable for VSCode
          mkdir -p /tmp/code
          touch /tmp/code/code
          chmod +x /tmp/code/code
          export PATH="/tmp/code:$PATH"\n"""

    if not ("automl" in folder):
        workflow_yaml += f"""
          papermill -k python {name}.ipynb {name}.output.ipynb
      working-directory: sdk/python/{posix_folder}"""
    elif "nlp" in folder or "image" in folder:
        # need GPU cluster, so override the compute cluster name to dedicated
        workflow_yaml += f"""          
          papermill -k python -p compute_name automl-gpu-cluster {name}.ipynb {name}.output.ipynb
      working-directory: sdk/python/{posix_folder}"""
    else:
        # need CPU cluster, so override the compute cluster name to dedicated
        workflow_yaml += f"""
          papermill -k python -p compute_name automl-cpu-cluster {name}.ipynb {name}.output.ipynb
      working-directory: sdk/python/{posix_folder}"""

    workflow_yaml += f"""
    - name: upload notebook's working folder as an artifact
      if: ${{{{ always() }}}}
      uses: actions/upload-artifact@v2
      with:
        name: {name}
        path: sdk/python/{posix_folder}\n"""

    workflow_yaml += f"""
    - name: Send IcM on failure
      if: ${{{{ failure() && github.ref_type == 'branch' && (github.ref_name == 'main' || contains(github.ref_name, 'release')) }}}}
      uses: ./.github/actions/generate-icm
      with:
        host: ${{{{ secrets.AZUREML_ICM_CONNECTOR_HOST_NAME }}}}
        connector_id: ${{{{ secrets.AZUREML_ICM_CONNECTOR_CONNECTOR_ID }}}}
        certificate: ${{{{ secrets.AZUREML_ICM_CONNECTOR_CERTIFICATE }}}}
        private_key: ${{{{ secrets.AZUREML_ICM_CONNECTOR_PRIVATE_KEY }}}}
        args: |
            incident:
                Title: "[azureml-examples] Notebook validation failed on branch '${{{{ github.ref_name }}}}' for notebook '{posix_notebook}'"
                Summary: |
                    Notebook '{posix_notebook}' is failing on branch '${{{{ github.ref_name }}}}': ${{{{ github.server_url }}}}/${{{{ github.repository }}}}/actions/runs/${{{{ github.run_id }}}}
                Severity: 4
                RoutingId: "github://azureml-examples"
                Status: Active
                Source:
                    IncidentId: "{posix_notebook}[${{{{ github.ref_name }}}}]"\n"""

    workflow_file = os.path.join(
        "..", "..", ".github", "workflows", f"sdk-{classification}-{name}.yml"
    )
    workflow_before = ""
    if os.path.exists(workflow_file):
        with open(workflow_file, "r") as f:
            workflow_before = f.read()

    if workflow_yaml != workflow_before:
        # write workflow
        with open(workflow_file, "w") as f:
            f.write(workflow_yaml)


def write_readme(notebooks, pipeline_folder=None):
    prefix = "prefix.md"
    suffix = "suffix.md"
    readme_file = "README.md"
    if pipeline_folder:
        prefix = os.path.join(pipeline_folder, prefix)
        suffix = os.path.join(pipeline_folder, suffix)
        readme_file = os.path.join(pipeline_folder, readme_file)

    if BRANCH == "":
        branch = "main"
    else:
        branch = BRANCH
        # read in prefix.md and suffix.md
        with open(prefix, "r") as f:
            prefix = f.read()
        with open(suffix, "r") as f:
            suffix = f.read()

        # define markdown tables
        notebook_table = f"Test Status is for branch - **_{branch}_**\n|Area|Sub-Area|Notebook|Description|Status|\n|--|--|--|--|--|\n"
        for notebook in notebooks:
            # get notebook name
            name = notebook.split(os.sep)[-1].replace(".ipynb", "")
            area = notebook.split(os.sep)[0]
            sub_area = notebook.split(os.sep)[1]
            folder = os.path.dirname(notebook)
            classification = folder.replace(os.sep, "-")

            try:
                # read in notebook
                with open(notebook, "r") as f:
                    data = json.load(f)

                description = "*no description*"
                try:
                    if data["metadata"]["description"] is not None:
                        description = data["metadata"]["description"]["description"]
                except:
                    pass
            except:
                print("Could not load", notebook)
                pass

            if any(excluded in notebook for excluded in NOT_TESTED_NOTEBOOKS):
                description += " - _This sample is excluded from automated tests_"
            if any(excluded in notebook for excluded in NOT_SCHEDULED_NOTEBOOKS):
                description += " - _This sample is only tested on demand_"

            if pipeline_folder:
                notebook = os.path.relpath(notebook, pipeline_folder)

            # write workflow file
            notebook_table += (
                write_readme_row(
                    branch,
                    notebook.replace(os.sep, "/"),
                    name,
                    classification,
                    area,
                    sub_area,
                    description,
                )
                + "\n"
            )

        print("writing README.md...")
        with open(readme_file, "w") as f:
            f.write(prefix + notebook_table + suffix)
        print("finished writing README.md")


def write_readme_row(
    branch, notebook, name, classification, area, sub_area, description
):
    gh_link = "https://github.com/Azure/azureml-examples/actions/workflows"

    nb_name = f"[{name}]({notebook})"
    status = f"[![{name}]({gh_link}/sdk-{classification}-{name}.yml/badge.svg?branch={branch})]({gh_link}/sdk-{classification}-{name}.yml)"

    row = f"|{area}|{sub_area}|{nb_name}|{description}|{status}|"
    return row


def modify_notebooks(notebooks):
    print("modifying notebooks...")
    # setup variables
    kernelspec = {
        "display_name": "Python 3.10 - SDK V2",
        "language": "python",
        "name": "python310-sdkv2",
    }

    # for each notebooks
    for notebook in notebooks:

        # read in notebook
        with open(notebook, "r", encoding="utf-8") as f:
            data = json.load(f)

        # update metadata
        data["metadata"]["kernelspec"] = kernelspec

        # write notebook
        with open(notebook, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
            f.write("\n")

    print("finished modifying notebooks...")


@contextlib.contextmanager
def change_working_dir(path):
    """Context manager for changing the current working directory"""

    saved_path = os.getcwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(saved_path)


# run functions
if __name__ == "__main__":

    # setup argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-readme", type=bool, default=False)
    args = parser.parse_args()

    # call main
    main(args)
