#include "../src/io.hpp"
#include <filesystem>
#include <fstream>
#include <iostream>
int main(int argc,char**argv){if(argc!=2)return 2;namespace fs=std::filesystem;const fs::path p=argv[1];fs::create_directory(p);
struct Cleanup{fs::path p;~Cleanup(){fs::remove_all(p);}} cleanup{p};
{std::ofstream out(p/"meminfo");out<<"MemTotal: 100000 kB\nMemAvailable: 50000 kB\nMemFree: 20000 kB\nSwapTotal: 100000 kB\nSwapFree: 90000 kB\n";}
const std::vector<std::pair<std::string,std::optional<ms::Bytes>>> cases={{"oom_kill_cnt 0\n",0},{"oom_kill_cnt 76\n",76},{"pswpout 1\n",std::nullopt},{"oom_kill_cnt bad\n",std::nullopt},{"oom_kill_cnt 18446744073709551616\n",std::nullopt}};
for(const auto& item:cases){{std::ofstream out(p/"vmstat");out<<item.first;}const auto s=ms::systemSample(p.string());if(s.oomKillCnt!=item.second)return 3;}
std::cout<<"OOM_COUNTER_LOGGING PASS zero/value/missing/malformed/overflow; diagnostic_only\n";return 0;}
