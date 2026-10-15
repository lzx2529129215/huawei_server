#pragma once
#include <atomic>
#include <condition_variable>
#include <deque>
#include <fstream>
#include <functional>
#include <mutex>
#include <thread>
#include <chrono>
#include <memory>
#include <stdexcept>
namespace async_ledger {
struct Job { std::string path; std::function<std::string()> serialize; std::size_t bytes; };
class Writer {
    std::mutex mutex_; std::condition_variable ready_;
    std::deque<Job> jobs_; std::size_t bytes_=0, count_=0;
    const std::size_t maxCount_,maxBytes_;
    bool closing_=false; std::atomic<bool> failed_{false};
    std::thread thread_; std::function<void()> failCallback_;
    std::function<bool(const std::string&,const std::string&)> sink_;
    void fail() { failed_=true; if(failCallback_)failCallback_(); }
    void run() {
        for(;;) {
            Job job;
            { std::unique_lock<std::mutex> lock(mutex_); ready_.wait(lock,[&]{return closing_||!jobs_.empty();});
              if(jobs_.empty())return;
              job=std::move(jobs_.front()); jobs_.pop_front(); }
            const auto start=std::chrono::steady_clock::now();
            try { const auto value=job.serialize(); if(!sink_(job.path,value))fail(); }
            catch(...) { fail(); }
            const double elapsed=std::chrono::duration<double>(std::chrono::steady_clock::now()-start).count();
            { std::lock_guard<std::mutex> lock(mutex_); --count_; bytes_-=job.bytes;
              ++written; backgroundSeconds+=elapsed; if(elapsed>backgroundMax)backgroundMax=elapsed; ready_.notify_all(); }
        }
    }
public:
    std::uint64_t written=0; double backgroundSeconds=0,backgroundMax=0;
    static bool fileSink(const std::string& path,const std::string& value) {
        std::ofstream out(path,std::ios::out|std::ios::app); if(!out)return false;
        out<<value<<'\n';out.flush(); return static_cast<bool>(out);
    }
    Writer(std::size_t count,std::size_t bytes,std::function<void()> failed,
           std::function<bool(const std::string&,const std::string&)> sink=fileSink)
        :maxCount_(count),maxBytes_(bytes),failCallback_(std::move(failed)),sink_(std::move(sink)) {
        thread_=std::thread([this]{run();});
    }
    Writer(const Writer&)=delete;Writer& operator=(const Writer&)=delete;
    ~Writer(){finish();}
    bool enqueue(std::string path,std::size_t bytes,std::function<std::string()> serialize) {
        std::lock_guard<std::mutex> lock(mutex_);
        if(failed_||closing_)return false;
        if(count_>=maxCount_ || bytes>maxBytes_ || bytes_>maxBytes_-bytes){fail();return false;}
        jobs_.push_back({std::move(path),std::move(serialize),bytes});++count_;bytes_+=bytes;
        ready_.notify_one();return true;
    }
    bool drain(){std::unique_lock<std::mutex> lock(mutex_);ready_.wait(lock,[&]{return count_==0;});return healthy();}
    bool healthy()const{return !failed_.load();}
    bool finish(){ {std::lock_guard<std::mutex> lock(mutex_);closing_=true;}ready_.notify_all();if(thread_.joinable())thread_.join();return healthy(); }
};
struct Ledger {
    Writer& writer; std::string path;
    Ledger(Writer& w,std::string p):writer(w),path(std::move(p)){}
    void deferred(std::size_t bytes,std::function<std::string()> build) {
        if(!writer.enqueue(path,bytes,std::move(build)))throw std::runtime_error("async_ledger_invalid_enqueue");
    }
    void row(std::string value) {
        const auto bytes=value.size();deferred(bytes,[value=std::move(value)]{return value;});
    }
};
}
