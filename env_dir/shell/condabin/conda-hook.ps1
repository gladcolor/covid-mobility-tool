$Env:CONDA_EXE = "/media/gpu/easystore/covid-mobility-tool/env_dir/bin/conda"
$Env:_CE_M = ""
$Env:_CE_CONDA = ""
$Env:_CONDA_ROOT = "/media/gpu/easystore/covid-mobility-tool/env_dir"
$Env:_CONDA_EXE = "/media/gpu/easystore/covid-mobility-tool/env_dir/bin/conda"
$CondaModuleArgs = @{ChangePs1 = $True}

Import-Module "$Env:_CONDA_ROOT\shell\condabin\Conda.psm1" -ArgumentList $CondaModuleArgs
Remove-Variable CondaModuleArgs